"""MemoryStore — structured key/value memory backed by Postgres.

Handles CRUD operations for explicit user knowledge (preferences, facts,
instructions, profile data, projects). Each user gets their own namespace.
"""

from __future__ import annotations

import logging

from .models import MemoryRecord

logger = logging.getLogger(__name__)


class MemoryStore:
    """Postgres-backed store for explicit user memories."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    def remember(
        self,
        key: str,
        value: str,
        *,
        type: str = "fact",
        source: str = "user",
        confidence: float = 1.0,
    ) -> MemoryRecord | None:
        """Save or update a memory. Returns the record, or None on error."""
        try:
            from .. import db as db_mod

            conn = db_mod.pooled_connect()
            try:
                row = db_mod.upsert_fact(
                    conn,
                    self.user_id,
                    type=type,
                    key=key,
                    value=value,
                    source=source,
                    confidence=confidence,
                )
                return MemoryRecord.from_row(row)
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("MemoryStore.remember failed: %s", exc)
            return None

    def recall(self, key: str) -> MemoryRecord | None:
        """Get a single memory by key."""
        try:
            from .. import db as db_mod

            conn = db_mod.pooled_connect()
            try:
                row = db_mod.get_fact(conn, self.user_id, key)
                return MemoryRecord.from_row(row) if row else None
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("MemoryStore.recall failed: %s", exc)
            return None

    def search(
        self,
        query: str,
        *,
        type: str | None = None,
        limit: int = 10,
    ) -> list[MemoryRecord]:
        """Search memories by key/value content."""
        try:
            from .. import db as db_mod

            conn = db_mod.pooled_connect()
            try:
                rows = db_mod.search_facts(conn, self.user_id, query, type=type, limit=limit)
                return [MemoryRecord.from_row(r) for r in rows]
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("MemoryStore.search failed: %s", exc)
            return []

    def forget(self, key: str) -> bool:
        """Delete a memory by key. Returns True if deleted."""
        try:
            from .. import db as db_mod

            conn = db_mod.pooled_connect()
            try:
                return db_mod.delete_fact(conn, self.user_id, key)
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("MemoryStore.forget failed: %s", exc)
            return False

    def list_all(self, *, type: str | None = None) -> list[MemoryRecord]:
        """List all memories, optionally filtered by type."""
        try:
            from .. import db as db_mod

            conn = db_mod.pooled_connect()
            try:
                rows = db_mod.list_facts(conn, self.user_id, type=type)
                return [MemoryRecord.from_row(r) for r in rows]
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("MemoryStore.list_all failed: %s", exc)
            return []
