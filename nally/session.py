"""Session persistence — bridges Agent <-> NEON.

One session per user (v1). If the user is logged in (auth.json exists)
and DATABASE_URL is set, the Agent's messages are loaded from Postgres
on startup and every new message is appended to the DB (ALL: user,
assistant, assistant+tool_calls, tool results, plus model/tokens/timestamps).

If not logged in or DB unavailable, persistence is silently disabled
and the Agent runs in-memory only (no crash).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def is_persistence_enabled() -> bool:
    """True if we should persist (logged in + DB configured)."""
    try:
        from . import auth as auth_mod
        from . import db as db_mod

        return bool(auth_mod.get_current_auth() and db_mod.is_configured())
    except Exception:
        return False


def get_session_store() -> SessionStore | None:
    """Return a SessionStore for the current user, or None if persistence disabled."""
    try:
        from . import auth as auth_mod
        from . import db as db_mod

        auth_data = auth_mod.get_current_auth()
        if auth_data is None:
            return None
        if not db_mod.is_configured():
            return None
        conn = db_mod.pooled_connect()
        try:
            user = db_mod.get_user_by_id(conn, auth_data["user_id"])
            if user is None:
                return None
            session = db_mod.get_or_create_session(conn, user["id"])
            return SessionStore(session_id=session["id"], user_id=user["id"])
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("get_session_store failed: %s", exc)
        return None


class SessionStore:
    """Thin wrapper around nally.db that knows the session_id and user_id."""

    def __init__(self, session_id: str, user_id: str | None = None) -> None:
        self.session_id = session_id
        self.user_id = user_id

    # ------------------------------------------------------------------ load
    def load(self) -> list[dict[str, Any]]:
        """Load all messages for this session (seq order). Returns [] on error."""
        try:
            from . import db as db_mod

            conn = db_mod.pooled_connect()
            try:
                return db_mod.load_messages(conn, self.session_id)
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("SessionStore.load failed: %s", exc)
            return []

    def load_with_meta(self) -> list[dict[str, Any]]:
        try:
            from . import db as db_mod

            conn = db_mod.pooled_connect()
            try:
                return db_mod.load_messages_with_meta(conn, self.session_id)
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("SessionStore.load_with_meta failed: %s", exc)
            return []

    def has_messages(self) -> bool:
        return bool(self.load())

    def info(self) -> dict[str, Any] | None:
        try:
            from . import db as db_mod

            conn = db_mod.pooled_connect()
            try:
                return db_mod.get_session(conn, self.session_id)
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("SessionStore.info failed: %s", exc)
            return None

    # ------------------------------------------------------------------ append
    def append(
        self,
        message: dict[str, Any],
        *,
        model: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> bool:
        """Persist a single message. Returns True on success, False on error (never raises)."""
        try:
            from . import db as db_mod

            conn = db_mod.pooled_connect()
            try:
                db_mod.append_message(
                    conn,
                    self.session_id,
                    message,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                )
            finally:
                conn.close()
            return True
        except Exception as exc:
            logger.debug("SessionStore.append failed: %s", exc)
            return False

    # ------------------------------------------------------------------ clear
    def clear(self, *, keep_system_prompt: str | None = None) -> bool:
        """Delete all messages and optionally re-insert the system prompt."""
        try:
            from . import db as db_mod

            conn = db_mod.pooled_connect()
            try:
                db_mod.clear_messages(conn, self.session_id)
                if keep_system_prompt is not None:
                    db_mod.append_message(
                        conn, self.session_id, {"role": "system", "content": keep_system_prompt}
                    )
            finally:
                conn.close()
            return True
        except Exception as exc:
            logger.debug("SessionStore.clear failed: %s", exc)
            return False

    def count(self) -> int:
        try:
            from . import db as db_mod

            conn = db_mod.pooled_connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM messages WHERE session_id = %s", (self.session_id,)
                    )
                    (n,) = cur.fetchone()
                    return int(n)
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("SessionStore.count failed: %s", exc)
            return 0
