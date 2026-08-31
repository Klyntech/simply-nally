"""Memory tools — explicit knowledge management for users.

These tools let the agent save, retrieve, search, and delete user
facts/preferences. Only available when a user_id is provided (persistence enabled).
"""

from __future__ import annotations

import logging
from typing import Any

from .base import Tool

logger = logging.getLogger(__name__)


class RememberTool(Tool):
    """Save a fact or preference about the user."""

    def __init__(self, user_id: str) -> None:
        super().__init__(
            name="remember",
            description=(
                "Save a fact, preference, or instruction about the user. "
                "Use when the user explicitly asks to remember something."
            ),
            parameters={
                "key": {
                    "type": "string",
                    "description": (
                        "Short identifier in snake_case, e.g. 'programming_language', "
                        "'code_editor', 'name'. Max 64 chars."
                    ),
                    "required": True,
                },
                "value": {
                    "type": "string",
                    "description": "The value to remember, e.g. 'TypeScript', 'VS Code'",
                    "required": True,
                },
                "type": {
                    "type": "string",
                    "description": "Category of memory",
                    "enum": ["preference", "fact", "instruction", "profile", "project"],
                    "required": False,
                },
            },
        )
        self.user_id = user_id

    def execute(self, key: str = "", value: str = "", type: str = "fact", **kwargs: Any) -> str:
        from ..memory.store import MemoryStore

        store = MemoryStore(self.user_id)
        record = store.remember(key=key, value=value, type=type)
        if record:
            return f"Remembered: {record.key} = {record.value} ({record.type.value})"
        return "Error: failed to save memory"


class RecallTool(Tool):
    """Look up a specific fact about the user."""

    def __init__(self, user_id: str) -> None:
        super().__init__(
            name="recall",
            description="Look up a specific fact or preference about the user by key.",
            parameters={
                "key": {
                    "type": "string",
                    "description": "The memory key to look up, e.g. 'programming_language'",
                    "required": True,
                },
            },
        )
        self.user_id = user_id

    def execute(self, key: str = "", **kwargs: Any) -> str:
        from ..memory.store import MemoryStore

        store = MemoryStore(self.user_id)
        record = store.recall(key)
        if record:
            return f"{record.key} = {record.value} ({record.type.value}, confidence: {record.confidence})"
        return f"No memory found for key '{key}'."


class SearchMemoryTool(Tool):
    """Search memories by keyword."""

    def __init__(self, user_id: str) -> None:
        super().__init__(
            name="search_memory",
            description="Search through stored memories by keyword in key or value.",
            parameters={
                "query": {
                    "type": "string",
                    "description": "Search term to match against keys and values",
                    "required": True,
                },
                "type_filter": {
                    "type": "string",
                    "description": "Optional type filter",
                    "enum": ["preference", "fact", "instruction", "profile", "project"],
                    "required": False,
                },
            },
        )
        self.user_id = user_id

    def execute(self, query: str = "", type_filter: str | None = None, **kwargs: Any) -> str:
        from ..memory.store import MemoryStore

        store = MemoryStore(self.user_id)
        results = store.search(query, type=type_filter, limit=5)
        if not results:
            return f"No memories found matching '{query}'."

        lines = [f"Found {len(results)} memor(y/ies):"]
        for m in results:
            lines.append(f"  - {m.key}: {m.value} ({m.type.value})")
        return "\n".join(lines)


class ForgetTool(Tool):
    """Delete a specific memory."""

    def __init__(self, user_id: str) -> None:
        super().__init__(
            name="forget",
            description="Delete a specific fact or preference about the user.",
            parameters={
                "key": {
                    "type": "string",
                    "description": "The memory key to forget, e.g. 'programming_language'",
                    "required": True,
                },
            },
        )
        self.user_id = user_id

    def execute(self, key: str = "", **kwargs: Any) -> str:
        from ..memory.store import MemoryStore

        store = MemoryStore(self.user_id)
        record = store.recall(key)
        if not record:
            return f"No memory found for key '{key}'."

        if store.forget(key):
            return f"Forgot: {record.key} = {record.value}"
        return f"Error: failed to forget memory '{key}'"


class ListMemoriesTool(Tool):
    """List all stored memories."""

    def __init__(self, user_id: str) -> None:
        super().__init__(
            name="list_memories",
            description="List all stored memories about the user.",
            parameters={
                "type_filter": {
                    "type": "string",
                    "description": "Optional type filter",
                    "enum": ["preference", "fact", "instruction", "profile", "project"],
                    "required": False,
                },
            },
        )
        self.user_id = user_id

    def execute(self, type_filter: str | None = None, **kwargs: Any) -> str:
        from ..memory.store import MemoryStore

        store = MemoryStore(self.user_id)
        memories = store.list_all(type=type_filter)
        if not memories:
            return "No memories stored yet."

        lines = [f"Stored memories ({len(memories)}):"]
        for m in memories:
            lines.append(f"  - {m.key}: {m.value} ({m.type.value})")
        return "\n".join(lines)


def register_memory_tools(registry: Any, user_id: str) -> None:
    """Register all memory tools bound to a user_id."""
    registry.register(RememberTool(user_id))
    registry.register(RecallTool(user_id))
    registry.register(SearchMemoryTool(user_id))
    registry.register(ForgetTool(user_id))
    registry.register(ListMemoriesTool(user_id))
