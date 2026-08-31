"""MemoryManager — high-level memory operations.

Handles:
- Dynamic per-message memory retrieval (relevant memories injected before LLM call)
- Explicit memory commands ("remember that...", "forget...", "what do you remember")
- System prompt augmentation with relevant facts
- Deterministic multi-pass retrieval with scoring
"""

from __future__ import annotations

import logging
import re

from .models import MemoryRecord, MemoryStoreError, MemoryType
from .store import MemoryStore

logger = logging.getLogger(__name__)

# Context injection limits
_MAX_CONTEXT_CHARS = 2000
_MAX_RECALL_MEMORIES = 10

# Retrieval scoring weights
_SCORE_EXACT_KEY = 100
_SCORE_KEY_TOKEN = 50
_SCORE_VALUE_ILIKE = 30
_SCORE_TYPE_MATCH = 10

# Patterns for explicit memory commands
_REMEMBER_PATTERNS = [
    re.compile(r"remember that (.+)", re.IGNORECASE),
    re.compile(r"save (?:that |this )?(.+)", re.IGNORECASE),
    re.compile(r"don'?t forget (?:that )?(.+)", re.IGNORECASE),
    re.compile(r"keep in mind (?:that )?(.+)", re.IGNORECASE),
]

_FORGET_PATTERNS = [
    re.compile(r"forget (?:that |about )?(.+)", re.IGNORECASE),
    re.compile(r"delete (?:that |the )?(?:memory |fact |preference )?(.+)", re.IGNORECASE),
    re.compile(r"remove (?:that |the )?(?:memory |fact |preference )?(.+)", re.IGNORECASE),
]

_RECALL_PATTERNS = [
    re.compile(r"what do you (?:know|remember) about me", re.IGNORECASE),
    re.compile(r"what (?:are |is )?my (?:memories|facts|preferences)", re.IGNORECASE),
    re.compile(r"show (?:me )?(?:my )?memories", re.IGNORECASE),
    re.compile(r"list (?:my )?memories", re.IGNORECASE),
]


class MemoryManager:
    """High-level memory interface for the Agent.

    Design:
      - remember: explicit user intent only (v1)
      - recall: automatic when relevant (before each LLM call)
      - forget: explicit user intent only
    """

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self._store = MemoryStore(user_id) if user_id else None

    @property
    def enabled(self) -> bool:
        """Memory is enabled if we have a user_id."""
        return bool(self.user_id)

    # ---------------------------------------------------------------- retrieval

    def get_relevant_memories(self, user_message: str, *, limit: int = 5) -> list[MemoryRecord]:
        """Retrieve memories relevant to the current user message.

        Multi-pass deterministic retrieval:
          1. Exact key match (score 100)
          2. Key token overlap (score 50 + overlap count)
          3. Value ILIKE match (score 30)
          4. Type-relevant defaults (score 10)

        No embeddings needed for v1.
        """
        if not self.enabled or self._store is None:
            return []

        words = re.findall(r"[a-zA-Z]{3,}", user_message)
        if not words:
            return []

        query_lower = user_message.lower()
        scored: dict[str, tuple[int, MemoryRecord]] = {}

        try:
            all_memories = self._store.list_all()
        except MemoryStoreError as exc:
            logger.warning("Memory retrieval failed: %s", exc)
            return []

        if not all_memories:
            return []

        for mem in all_memories:
            score = 0

            # Pass 1: exact key match
            if mem.key.lower() in query_lower:
                score += _SCORE_EXACT_KEY

            # Pass 2: key token overlap
            key_tokens = set(re.findall(r"[a-zA-Z]{3,}", mem.key.lower()))
            query_tokens = set(w.lower() for w in words)
            overlap = key_tokens & query_tokens
            if overlap:
                score += _SCORE_KEY_TOKEN + len(overlap) * 10

            # Pass 3: value ILIKE match
            value_lower = mem.value.lower()
            for word in words[:5]:
                if word.lower() in value_lower:
                    score += _SCORE_VALUE_ILIKE
                    break

            # Pass 4: type relevance
            if mem.type == MemoryType.INSTRUCTION:
                score += _SCORE_TYPE_MATCH

            if score > 0:
                existing = scored.get(mem.key)
                if existing is None or score > existing[0]:
                    scored[mem.key] = (score, mem)

        # Sort by score descending, then by recency
        ranked = sorted(scored.values(), key=lambda x: (-x[0], x[1].updated_at or ""))
        return [mem for _, mem in ranked[:limit]]

    def build_context_block(self, user_message: str) -> str:
        """Build a formatted string of relevant memories for system prompt injection.

        Returns empty string if no memories or memory disabled.
        Enforces _MAX_CONTEXT_CHARS limit.
        """
        if not self.enabled:
            return ""

        memories = self.get_relevant_memories(user_message)
        if not memories:
            return ""

        lines = ["## Known Facts About This User"]
        for m in memories:
            lines.append(m.to_context_line())

        block = "\n".join(lines)
        if len(block) > _MAX_CONTEXT_CHARS:
            block = block[:_MAX_CONTEXT_CHARS] + "\n... [truncated]"

        return block

    # -------------------------------------------------------- command detection

    def handle_memory_command(self, text: str) -> str | None:
        """Check if the user is asking to remember/forget/recall.

        Returns a response string if it was a memory command, None otherwise.
        The agent should check this BEFORE calling the LLM.
        """
        if not self.enabled:
            return None

        stripped = text.strip()

        # Check recall commands
        for pat in _RECALL_PATTERNS:
            if pat.search(stripped):
                return self._handle_recall()

        # Check forget commands
        for pat in _FORGET_PATTERNS:
            m = pat.search(stripped)
            if m:
                return self._handle_forget(m.group(1).strip())

        # Check remember commands
        for pat in _REMEMBER_PATTERNS:
            m = pat.search(stripped)
            if m:
                return self._handle_remember(m.group(1).strip())

        return None

    # -------------------------------------------------------- command handlers

    def _handle_remember(self, statement: str) -> str:
        """Parse and store a 'remember that...' statement."""
        key, value, mem_type = self._parse_statement(statement)

        try:
            record = self._store.remember(key=key, value=value, type=mem_type)
            return f"Remembered: {record.key} = {record.value} ({record.type.value})"
        except MemoryStoreError as exc:
            logger.warning("Memory save failed: %s", exc)
            return "Failed to save memory: storage unavailable. Please try again."

    def _handle_forget(self, target: str) -> str:
        """Forget a memory by key or value search."""
        try:
            # First try direct key lookup
            record = self._store.recall(target)
            if record:
                self._store.forget(record.key)
                return f"Forgot: {record.key} = {record.value}"

            # Try searching by value
            results = self._store.search(target, limit=1)
            if results:
                self._store.forget(results[0].key)
                return f"Forgot: {results[0].key} = {results[0].value}"

            return f"I don't have any memory matching '{target}'."
        except MemoryStoreError as exc:
            logger.warning("Memory forget failed: %s", exc)
            return "Failed to access memory: storage unavailable."

    def _handle_recall(self) -> str:
        """List all stored memories."""
        try:
            all_memories = self._store.list_all()
            if not all_memories:
                return "I don't have any memories stored yet."

            lines = ["What I know about you:"]
            for m in all_memories[:_MAX_RECALL_MEMORIES]:
                lines.append(f"  - {m.key}: {m.value} ({m.type.value})")
            if len(all_memories) > _MAX_RECALL_MEMORIES:
                lines.append(f"  ... and {len(all_memories) - _MAX_RECALL_MEMORIES} more")
            return "\n".join(lines)
        except MemoryStoreError as exc:
            logger.warning("Memory recall failed: %s", exc)
            return "Failed to access memory: storage unavailable."

    # -------------------------------------------------------- statement parsing

    def _parse_statement(self, statement: str) -> tuple[str, str, MemoryType]:
        """Parse a natural language statement into (key, value, type).

        Examples:
            "I prefer TypeScript" -> ("programming_language", "TypeScript", PREFERENCE)
            "My name is Alex" -> ("name", "Alex", PROFILE)
            "I use VS Code" -> ("code_editor", "VS Code", PREFERENCE)
        """
        # Pattern: "I prefer X" / "I like X" / "I use X"
        m = re.match(r"i (?:prefer|like|use|want) (.+)", statement, re.IGNORECASE)
        if m:
            value = m.group(1).strip().rstrip(".")
            key = self._infer_key_from_value(value, statement.lower())
            return (key, value, MemoryType.PREFERENCE)

        # Pattern: "My X is Y"
        m = re.match(r"my (.+?) is (.+)", statement, re.IGNORECASE)
        if m:
            key_part = m.group(1).strip()
            value = m.group(2).strip().rstrip(".")
            key = self._normalize_key_part(key_part)
            mem_type = self._infer_type_from_key(key)
            return (key, value, mem_type)

        # Fallback: use the whole statement as value, derive key from content
        value = statement.strip().rstrip(".")
        key = self._infer_key_from_value(value, statement.lower())
        mem_type = self._infer_type_from_key(key)
        return (key, value, mem_type)

    def _infer_key_from_value(self, value: str, context: str = "") -> str:
        """Infer a reasonable memory key from the value and surrounding context."""
        value_lower = value.lower()

        # Common mappings
        key_hints = {
            "python": "programming_language",
            "javascript": "programming_language",
            "typescript": "programming_language",
            "rust": "programming_language",
            "go": "programming_language",
            "java": "programming_language",
            "c++": "programming_language",
            "ruby": "programming_language",
            "php": "programming_language",
            "swift": "programming_language",
            "kotlin": "programming_language",
            "vs code": "code_editor",
            "vim": "code_editor",
            "neovim": "code_editor",
            "emacs": "code_editor",
            "pycharm": "code_editor",
            "intellij": "code_editor",
        }

        for hint, key in key_hints.items():
            if hint in value_lower:
                return key

        # Check context for key hints
        if "language" in context:
            return "programming_language"
        if "editor" in context:
            return "code_editor"
        if "tool" in context:
            return "preferred_tool"

        # Fallback: generate key from first meaningful word
        words = re.findall(r"[a-zA-Z]+", value_lower)
        if words:
            return self._normalize_key_part(words[0])
        return "general"

    def _normalize_key_part(self, text: str) -> str:
        """Normalize a key fragment to snake_case."""
        k = text.strip().lower().replace(" ", "_").replace("-", "_")
        k = re.sub(r"[^a-z0-9_]", "", k)
        k = re.sub(r"_+", "_", k).strip("_")
        return k[:64] if k else "general"

    def _infer_type_from_key(self, key: str) -> MemoryType:
        """Infer memory type from the key name."""
        key_lower = key.lower()
        if "name" in key_lower or "age" in key_lower or "location" in key_lower:
            return MemoryType.PROFILE
        if "language" in key_lower or "editor" in key_lower or "tool" in key_lower:
            return MemoryType.PREFERENCE
        return MemoryType.FACT
