"""MemoryStore — structured key/value memory backed by Postgres.

Handles CRUD operations for explicit user knowledge (preferences, facts,
instructions, profile data, projects). Each user gets their own namespace.

Infrastructure failures raise MemoryStoreError instead of returning None/[],
so callers can distinguish "no result" from "store broken."
"""

from __future__ import annotations

import logging

from .models import MemoryRecord, MemoryStoreError, MemoryType

logger = logging.getLogger(__name__)

_MAX_VALUE_LEN = 500


class MemoryStore:
    """Postgres-backed store for explicit user memories."""

    def __init__(self, user_id: str) -> None:
        if not user_id:
            raise ValueError("MemoryStore requires a non-empty user_id")
        self.user_id = user_id

    def remember(
        self,
        key: str,
        value: str,
        *,
        type: MemoryType = MemoryType.FACT,
        source: str = "user",
    ) -> MemoryRecord:
        """Save or update a memory. Returns the record.

        Raises MemoryStoreError on infrastructure failure.
        Raises ValueError on invalid type.
        """
        if not key or not key.strip():
            raise ValueError("Memory key must not be empty")
        if len(value) > _MAX_VALUE_LEN:
            value = value[:_MAX_VALUE_LEN]
        try:
            from .. import db as db_mod

            conn = db_mod.pooled_connect()
            try:
                row = db_mod.upsert_fact(
                    conn,
                    self.user_id,
                    type=type.value,
                    key=key,
                    value=value,
                    source=source,
                )
                return MemoryRecord.from_row(row)
            finally:
                conn.close()
        except MemoryStoreError:
            raise
        except Exception as exc:
            raise MemoryStoreError(f"remember failed: {exc}") from exc

    def recall(self, key: str) -> MemoryRecord | None:
        """Get a single memory by key. Returns None if not found.

        Raises MemoryStoreError on infrastructure failure.
        """
        try:
            from .. import db as db_mod

            conn = db_mod.pooled_connect()
            try:
                row = db_mod.get_fact(conn, self.user_id, key)
                return MemoryRecord.from_row(row) if row else None
            finally:
                conn.close()
        except MemoryStoreError:
            raise
        except Exception as exc:
            raise MemoryStoreError(f"recall failed: {exc}") from exc

    def search(
        self,
        query: str,
        *,
        type: MemoryType | None = None,
        limit: int = 10,
    ) -> list[MemoryRecord]:
        """Search memories by key/value content.

        Raises MemoryStoreError on infrastructure failure.
        """
        try:
            from .. import db as db_mod

            conn = db_mod.pooled_connect()
            try:
                type_val = type.value if type is not None else None
                rows = db_mod.search_facts(conn, self.user_id, query, type=type_val, limit=limit)
                return [MemoryRecord.from_row(r) for r in rows]
            finally:
                conn.close()
        except MemoryStoreError:
            raise
        except Exception as exc:
            raise MemoryStoreError(f"search failed: {exc}") from exc

    def forget(self, key: str) -> bool:
        """Delete a memory by key. Returns True if deleted.

        Raises MemoryStoreError on infrastructure failure.
        """
        try:
            from .. import db as db_mod

            conn = db_mod.pooled_connect()
            try:
                return db_mod.delete_fact(conn, self.user_id, key)
            finally:
                conn.close()
        except MemoryStoreError:
            raise
        except Exception as exc:
            raise MemoryStoreError(f"forget failed: {exc}") from exc

    def list_all(self, *, type: MemoryType | None = None) -> list[MemoryRecord]:
        """List all memories, optionally filtered by type.

        Raises MemoryStoreError on infrastructure failure.
        """
        try:
            from .. import db as db_mod

            conn = db_mod.pooled_connect()
            try:
                type_val = type.value if type is not None else None
                rows = db_mod.list_facts(conn, self.user_id, type=type_val)
                return [MemoryRecord.from_row(r) for r in rows]
            finally:
                conn.close()
        except MemoryStoreError:
            raise
        except Exception as exc:
            raise MemoryStoreError(f"list_all failed: {exc}") from exc
