"""UserDirectory — canonical internal user identity and account linking.

Single source of truth: `users` table UUID primary key.
External identities (telegram, local_cli, provider subject) link to internal user via
`external_identities` table. No code should use Telegram ID or provider subject
as a token-store primary key directly.

This replaces ad-hoc `get_user_by_telegram_id`/`google_id` lookups scattered
across db.py and auth.py.

When DATABASE_URL not set (local dev without NEON), falls back to file-based
auth.json for CLI identity and in-memory map for Telegram (requires DB).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class DirectoryError(Exception):
    pass


class UserDirectory:
    """Canonical user identity management."""

    def _require_db(self):
        try:
            from nally import db

            if not db.is_configured():
                raise DirectoryError("DATABASE_URL not set — UserDirectory requires DB")
            return db
        except DirectoryError:
            raise
        except Exception as exc:
            raise DirectoryError(f"DB not available: {exc}") from exc

    # ------------------------------------------------------------------
    # Resolve / create
    # ------------------------------------------------------------------

    def get_or_create_for_telegram(self, telegram_id: str, username: str | None = None, first_name: str | None = None, display_name: str | None = None) -> dict[str, Any]:
        """Resolve internal user_id for a Telegram identity, creating if needed."""
        db = self._require_db()
        tid = str(telegram_id).strip()
        conn = db.pooled_connect()
        try:
            # Check external_identities first
            user = self._lookup_external(conn, "telegram", None, tid)
            if user:
                return user
            # Check legacy users.telegram_id (for migration)
            legacy = db.get_user_by_telegram_id(conn, tid)
            if legacy:
                # Backfill external_identities
                self._ensure_external(conn, legacy["id"], "telegram", None, tid, display_name or username or first_name)
                return legacy
            # Create new user via db.create_user_by_telegram
            new_user = db.create_user_by_telegram(conn, telegram_id=tid, username=username, first_name=first_name)
            self._ensure_external(conn, new_user["id"], "telegram", None, tid, display_name or username or first_name)
            # Ensure session
            db.get_or_create_session(conn, new_user["id"])
            return new_user
        finally:
            conn.close()

    def get_or_create_for_cli(self, local_id: str = "default", display_name: str | None = None) -> dict[str, Any]:
        """Resolve internal user for CLI local profile (file-based local_cli identity)."""
        # CLI local profile is stored in auth.json; we link it to DB user if DB available
        try:
            from nally.auth import get_current_auth

            auth = get_current_auth()
            if auth and auth.get("user_id"):
                db = self._require_db()
                conn = db.pooled_connect()
                try:
                    user = db.get_user_by_id(conn, auth["user_id"])
                    if user:
                        # Ensure external identity linked
                        self._ensure_external(conn, user["id"], "local_cli", None, str(local_id), display_name)
                        return user
                finally:
                    conn.close()
        except Exception:
            pass
        # If DB not configured or no auth, we still need a user — create via telegram-like helper
        # For CLI without DB, return a synthetic user dict
        db_available = False
        try:
            from nally import db

            db_available = db.is_configured()
        except Exception:
            db_available = False
        if not db_available:
            # Synthetic in-memory user for offline CLI
            import uuid

            uid = f"local-{local_id}"
            return {
                "id": uid,
                "google_id": None,
                "telegram_id": None,
                "email": f"{local_id}@local",
                "name": display_name or local_id,
                "picture": None,
            }
        # DB available but no auth — create user for this local_cli
        db = self._require_db()
        conn = db.pooled_connect()
        try:
            existing = self._lookup_external(conn, "local_cli", None, str(local_id))
            if existing:
                return existing
            # Create via generic insert
            import uuid

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (email, name)
                    VALUES (%s, %s)
                    RETURNING id::text, google_id, telegram_id, email, name, picture, created_at, last_login
                    """,
                    (f"cli_{local_id}@local", display_name or local_id),
                )
                row = cur.fetchone()
                cols = ["id", "google_id", "telegram_id", "email", "name", "picture", "created_at", "last_login"]
                user = dict(zip(cols, row, strict=False))
                conn.commit()
                self._ensure_external(conn, user["id"], "local_cli", None, str(local_id), display_name)
                db.get_or_create_session(conn, user["id"])
                return user
        finally:
            conn.close()

    def get_by_external(self, kind: str, provider: str | None, subject: str) -> dict[str, Any] | None:
        db = self._require_db()
        conn = db.pooled_connect()
        try:
            return self._lookup_external(conn, kind, provider, str(subject))
        finally:
            conn.close()

    def link_provider_identity(
        self, user_id: str, provider: str, subject: str, display_name: str | None = None
    ) -> None:
        db = self._require_db()
        conn = db.pooled_connect()
        try:
            self._ensure_external(conn, user_id, "provider", provider, str(subject), display_name)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # External identities helpers
    # ------------------------------------------------------------------

    def _lookup_external(self, conn, kind: str, provider: str | None, subject: str) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id::text, u.google_id, u.telegram_id, u.email, u.name, u.picture, u.created_at, u.last_login
                FROM users u
                JOIN external_identities e ON e.user_id = u.id
                WHERE e.kind = %s AND COALESCE(e.provider, '') = COALESCE(%s, '') AND e.subject = %s
                LIMIT 1
                """,
                (kind, provider, subject),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = ["id", "google_id", "telegram_id", "email", "name", "picture", "created_at", "last_login"]
            return dict(zip(cols, row, strict=False))

    def _ensure_external(self, conn, user_id: str, kind: str, provider: str | None, subject: str, display_name: str | None) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO external_identities (user_id, kind, provider, subject, display_name)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, kind, provider, subject) DO UPDATE SET display_name = EXCLUDED.display_name
                """,
                (user_id, kind, provider, subject, display_name),
            )
            conn.commit()

    def list_identities(self, user_id: str) -> list[dict[str, Any]]:
        db = self._require_db()
        conn = db.pooled_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id::text, user_id::text, kind, provider, subject, display_name, created_at FROM external_identities WHERE user_id = %s",
                    (user_id,),
                )
                rows = cur.fetchall()
                cols = ["id", "user_id", "kind", "provider", "subject", "display_name", "created_at"]
                return [dict(zip(cols, r, strict=False)) for r in rows]
        finally:
            conn.close()


# Singleton
_default_dir: UserDirectory | None = None


def get_directory() -> UserDirectory:
    global _default_dir
    if _default_dir is None:
        _default_dir = UserDirectory()
    return _default_dir
