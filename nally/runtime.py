"""Application runtime — owns Agent lifecycle, session management, identity resolution.

This is the boundary between transport layers (Telegram, CLI, future web)
and the NALLY core (Agent, Session, DB). Transports call into Runtime;
they never touch db, auth, session, or agent construction directly.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class UserStatus:
    """Normalized status for a user identity."""

    is_linked: bool = False
    email: str = ""
    google_id: str = ""
    telegram_id: str = ""
    session_id: str = ""
    message_count: int = 0
    total_tokens: int = 0
    agent_message_count: int = 0
    db_configured: bool = False


class Runtime:
    """Manages Agent lifecycle and identity resolution.

    Key design:
      - Identity is keyed by transport-level ID (e.g. telegram_user_id).
      - Agent cache is keyed by chat_id (per-chat isolation).
      - Runtime owns the DB access, session creation, and agent construction.
      - Transports never import nally.db, nally.auth, nally.session, or nally.agent.
    """

    def __init__(self, max_agents: int = 100) -> None:
        self._agents: OrderedDict[int, Any] = OrderedDict()
        self._max_agents = max_agents

    # ------------------------------------------------------------------ agent lifecycle

    def get_agent(self, chat_id: int, telegram_user_id: str | None = None) -> Any:
        """Return or create Agent for this chat.

        If telegram_user_id is provided and linked to a Google account,
        the Agent gets a SessionStore-backed Conversation (shared CLI history).
        Otherwise, the Agent runs in-memory only.
        """
        if chat_id in self._agents:
            self._agents.move_to_end(chat_id)
            return self._agents[chat_id]

        session_store = self._resolve_session(telegram_user_id)

        try:
            from nally.agent import Agent as _Agent
            from nally.conversation import Conversation

            if session_store is not None:
                conversation = Conversation(
                    system_prompt="",
                    session_store=session_store,
                    auto_persist=False,
                )
            else:
                conversation = Conversation(
                    system_prompt="",
                    auto_persist=False,
                )
            agent = _Agent(conversation=conversation)
        except Exception:
            from nally.agent import Agent as _Agent

            agent = _Agent()

        # Evict LRU if at capacity
        if len(self._agents) >= self._max_agents:
            self._agents.popitem(last=False)

        self._agents[chat_id] = agent
        return agent

    def clear_agent(self, chat_id: int) -> None:
        """Remove cached agent (e.g. after unlink, /clear)."""
        self._agents.pop(chat_id, None)

    def _resolve_session(self, telegram_user_id: str | None) -> Any:
        """Look up linked Google user and return SessionStore, or None."""
        if telegram_user_id is None:
            return None
        try:
            from nally import db
            from nally.session import SessionStore

            if not db.is_configured():
                return None
            conn = db.pooled_connect()
            try:
                user = db.get_user_by_telegram_id(conn, str(telegram_user_id))
                if user is None:
                    return None
                sess = db.get_or_create_session(conn, user["id"])
                return SessionStore(session_id=sess["id"], user_id=user["id"])
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("runtime: session resolution failed: %s", exc)
            return None

    # ------------------------------------------------------------------ identity queries

    def is_linked(self, telegram_user_id: str) -> bool:
        """Check if a Telegram user is linked to a Google account."""
        try:
            from nally import db

            if not db.is_configured():
                return False
            conn = db.pooled_connect()
            try:
                user = db.get_user_by_telegram_id(conn, telegram_user_id)
                return user is not None
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("runtime: is_linked check failed: %s", exc)
            return False

    def get_user_info(self, telegram_user_id: str) -> dict[str, Any] | None:
        """Return user dict from DB, or None if not found."""
        try:
            from nally import db

            if not db.is_configured():
                return None
            conn = db.pooled_connect()
            try:
                return db.get_user_by_telegram_id(conn, telegram_user_id)
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("runtime: get_user_info failed: %s", exc)
            return None

    def get_status(self, telegram_id: str, chat_id: int | None = None) -> UserStatus:
        """Return normalized status for a Telegram user."""
        status = UserStatus(telegram_id=telegram_id)

        try:
            from nally import db

            status.db_configured = db.is_configured()
        except Exception:
            status.db_configured = False
            return status

        if not status.db_configured:
            return status

        try:
            conn = db.pooled_connect()
            try:
                user = db.get_user_by_telegram_id(conn, telegram_id)
                if user is None:
                    return status

                status.is_linked = True
                status.email = user.get("email", "")
                status.google_id = user.get("google_id", "")

                sess = db.get_or_create_session(conn, user["id"])
                status.session_id = sess["id"]
                status.message_count = sess.get("message_count", 0)
                status.total_tokens = sess.get("total_tokens", 0)

                # Include agent message count if agent is cached
                if chat_id is not None:
                    agent = self._agents.get(chat_id)
                    if agent is not None:
                        status.agent_message_count = len(agent.messages)
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("runtime: get_status failed: %s", exc)

        return status

    # ------------------------------------------------------------------ state operations

    def clear_history(self, chat_id: int, telegram_user_id: str | None = None) -> bool:
        """Clear conversation history for this chat/user. Returns True on success."""
        # Try clearing via cached agent first
        agent = self._agents.get(chat_id)
        if agent is not None:
            try:
                agent.clear_history()
                return True
            except Exception as exc:
                logger.debug("runtime: agent clear failed: %s", exc)

        # Fall back to DB clear
        if telegram_user_id is None:
            return False
        try:
            from nally import db
            from nally.config import get_system_prompt
            from nally.session import SessionStore

            if not db.is_configured():
                return False
            conn = db.pooled_connect()
            try:
                user = db.get_user_by_telegram_id(conn, telegram_user_id)
                if user is None:
                    return False
                sess = db.get_or_create_session(conn, user["id"])
                store = SessionStore(session_id=sess["id"], user_id=user["id"])
                store.clear(keep_system_prompt=get_system_prompt())
                return True
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("runtime: db clear failed: %s", exc)
            return False

    def unlink(self, telegram_user_id: str) -> bool:
        """Unlink Telegram from Google. Returns True if was linked."""
        try:
            from nally import db

            if not db.is_configured():
                return False
            conn = db.pooled_connect()
            try:
                ok = db.unlink_telegram(conn, telegram_user_id)
                return ok
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("runtime: unlink failed: %s", exc)
            return False

    # ------------------------------------------------------------------ MCP status

    def get_mcp_status(self, user_id: str | None = None) -> dict[str, Any]:
        """Return normalized MCP status (no Telegram UI, no DB).

        If user_id is provided, checks per-user connection status.
        Otherwise returns global status for backwards compatibility.
        """
        result: dict[str, Any] = {}

        try:
            from nally.config import MCP_ENABLED

            result["mcp_enabled"] = MCP_ENABLED
        except Exception:
            result["mcp_enabled"] = False

        try:
            from nally.tools.mcp.adapter import _has_mcp

            result["mcp_package_installed"] = _has_mcp()
        except Exception:
            result["mcp_package_installed"] = False

        if user_id:
            try:
                from nally.integrations import IntegrationManager

                manager = IntegrationManager()
                status = manager.status(user_id)
                result["github_authenticated"] = status.get("github", {}).get("connected", False)
                result["gmail_authenticated"] = status.get("gmail", {}).get("connected", False)
                result["notion_authenticated"] = status.get("notion", {}).get("connected", False)
            except Exception:
                result["github_authenticated"] = False
                result["gmail_authenticated"] = False
                result["notion_authenticated"] = False
        else:
            # Fallback: check global tokens (env vars)
            result["github_authenticated"] = self.is_github_authenticated()
            result["gmail_authenticated"] = self.is_gmail_authenticated()
            result["notion_authenticated"] = self.is_notion_authenticated()

        return result

    def is_github_authenticated(self, user_id: str | None = None) -> bool:
        """Check if GitHub is authenticated for MCP."""
        if user_id:
            try:
                from nally.integrations import IntegrationManager

                return IntegrationManager().is_connected(user_id, "github")
            except Exception:
                return False
        try:
            from nally.github_oauth import is_github_authenticated

            return is_github_authenticated()
        except Exception:
            return False

    def is_gmail_authenticated(self, user_id: str | None = None) -> bool:
        """Check if Gmail is authenticated for MCP."""
        if user_id:
            try:
                from nally.integrations import IntegrationManager

                return IntegrationManager().is_connected(user_id, "gmail")
            except Exception:
                return False
        # Fallback: check env vars
        import os

        env_token = (
            os.getenv("GMAIL_TOKEN", "").strip()
            or os.getenv("GMAIL_OAUTH_TOKEN", "").strip()
            or os.getenv("GOOGLE_GMAIL_TOKEN", "").strip()
        )
        if env_token:
            return True
        try:
            from nally.integrations.token_store import get_valid_token

            return get_valid_token("_global", "gmail") is not None
        except Exception:
            return False

    def is_notion_authenticated(self, user_id: str | None = None) -> bool:
        """Check if Notion is authenticated for MCP."""
        if user_id:
            try:
                from nally.integrations import IntegrationManager

                return IntegrationManager().is_connected(user_id, "notion")
            except Exception:
                return False
        # Fallback: check env vars
        import os

        env_token = os.getenv("NOTION_TOKEN", "").strip()
        if env_token:
            return True
        try:
            from nally.integrations.token_store import get_valid_token

            return get_valid_token("_global", "notion") is not None
        except Exception:
            return False


# Module-level singleton
default_runtime = Runtime()
