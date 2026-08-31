"""Memory models — structured types for explicit user knowledge."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class MemoryType(StrEnum):
    PREFERENCE = "preference"
    FACT = "fact"
    INSTRUCTION = "instruction"
    PROFILE = "profile"
    PROJECT = "project"


class MemoryStoreError(Exception):
    """Raised when a memory storage operation fails (DB down, connection lost, etc.).

    Distinguished from "no result" — a failed recall() means the store is broken,
    not that the key doesn't exist.
    """


@dataclass
class MemoryRecord:
    """A single memory entry — explicit knowledge about a user."""

    id: str
    user_id: str
    type: MemoryType
    key: str
    value: str
    source: str = "user"
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict) -> MemoryRecord:
        """Construct from a database row dict."""
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            type=MemoryType(row["type"]),
            key=row["key"],
            value=row["value"],
            source=row.get("source", "user"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    def to_context_line(self) -> str:
        """Format for system prompt injection."""
        return f"- {self.key}: {self.value} ({self.type.value})"
