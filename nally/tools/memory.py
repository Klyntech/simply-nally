"""Memory tools — explicit knowledge management for users.

These tools let the agent save, retrieve, search, and delete user
facts/preferences. Only available when a user_id is provided (persistence enabled).

Tools receive a shared MemoryStore instance via constructor injection.
"""

from __future__ import annotations

import logging
from typing import Any

from ..memory.models import MemoryStoreError, MemoryType
from ..memory.store import MemoryStore
from .base import Tool

logger = logging.getLogger(__name__)


class RememberTool(Tool):
    """Save a fact or preference about the user."""

    def __init__(self, store: MemoryStore) -> None:
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
        self._store = store

    def execute(self, key: str = "", value: str = "", type: str = "fact", **kwargs: Any) -> str:
        try:
            memory_type = MemoryType(type)
            record = self._store.remember(key=key, value=value, type=memory_type)
            return f"Remembered: {record.key} = {record.value} ({record.type.value})"
        except MemoryStoreError:
            return "Error: memory storage unavailable"
        except ValueError:
            return f"Error: invalid memory type '{type}'"


class RecallTool(Tool):
    """Look up a specific fact about the user."""

    def __init__(self, store: MemoryStore) -> None:
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
        self._store = store

    def execute(self, key: str = "", **kwargs: Any) -> str:
        try:
            record = self._store.recall(key)
            if record:
                return f"{record.key} = {record.value} ({record.type.value})"
            return f"No memory found for key '{key}'."
        except MemoryStoreError:
            return "Error: memory storage unavailable"


class SearchMemoryTool(Tool):
    """Search memories by keyword."""

    def __init__(self, store: MemoryStore) -> None:
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
        self._store = store

    def execute(self, query: str = "", type_filter: str | None = None, **kwargs: Any) -> str:
        try:
            type_val = MemoryType(type_filter) if type_filter else None
            results = self._store.search(query, type=type_val, limit=5)
            if not results:
                return f"No memories found matching '{query}'."

            lines = [f"Found {len(results)} memor(y/ies):"]
            for m in results:
                lines.append(f"  - {m.key}: {m.value} ({m.type.value})")
            return "\n".join(lines)
        except MemoryStoreError:
            return "Error: memory storage unavailable"
        except ValueError:
            return f"Error: invalid type filter '{type_filter}'"


class ForgetTool(Tool):
    """Delete a specific memory."""

    def __init__(self, store: MemoryStore) -> None:
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
        self._store = store

    def execute(self, key: str = "", **kwargs: Any) -> str:
        try:
            record = self._store.recall(key)
            if not record:
                return f"No memory found for key '{key}'."

            if self._store.forget(key):
                return f"Forgot: {record.key} = {record.value}"
            return f"Error: failed to forget memory '{key}'"
        except MemoryStoreError:
            return "Error: memory storage unavailable"


class ListMemoriesTool(Tool):
    """List all stored memories."""

    def __init__(self, store: MemoryStore) -> None:
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
        self._store = store

    def execute(self, type_filter: str | None = None, **kwargs: Any) -> str:
        try:
            type_val = MemoryType(type_filter) if type_filter else None
            memories = self._store.list_all(type=type_val)
            if not memories:
                return "No memories stored yet."

            lines = [f"Stored memories ({len(memories)}):"]
            for m in memories:
                lines.append(f"  - {m.key}: {m.value} ({m.type.value})")
            return "\n".join(lines)
        except MemoryStoreError:
            return "Error: memory storage unavailable"
        except ValueError:
            return f"Error: invalid type filter '{type_filter}'"


def register_memory_tools(registry: Any, user_id: str) -> None:
    """Register all memory tools bound to a user_id."""
    store = MemoryStore(user_id)
    registry.register(RememberTool(store))
    registry.register(RecallTool(store))
    registry.register(SearchMemoryTool(store))
    registry.register(ForgetTool(store))
    registry.register(ListMemoriesTool(store))
