"""Conversation — message list + optional persistence.

Owns the message history and optionally bridges to a SessionStore (NEON).
Agent never touches persistence directly; it calls conversation.append().
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class Conversation:
    """Manages an ordered list of messages with optional persistence."""

    def __init__(
        self,
        *,
        system_prompt: str,
        session_store: Any | None = None,
        auto_persist: bool = True,
        default_model: str | None = None,
    ) -> None:
        self._session_store = session_store
        self._default_model = default_model
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

        # Auto-discover session store if not provided
        if self._session_store is None and auto_persist:
            try:
                from .session import get_session_store

                self._session_store = get_session_store()
            except Exception as exc:
                logger.debug("Session store discovery failed: %s", exc)
                self._session_store = None

        # Load existing history from store (replaces the fresh system prompt)
        if self._session_store is not None:
            try:
                loaded = self._session_store.load()
                if loaded:
                    self.messages = loaded
                else:
                    # First run — persist system prompt so DB isn't empty
                    self._session_store.append(self.messages[0])
            except Exception as exc:
                logger.warning("Session load failed: %s", exc)

    # ---------------------------------------------------------------- properties

    @property
    def is_persisting(self) -> bool:
        return self._session_store is not None

    @property
    def session_id(self) -> str | None:
        if self._session_store is not None:
            return getattr(self._session_store, "session_id", None)
        return None

    # ------------------------------------------------------------------ public

    def append(self, message: dict[str, Any], response: Any | None = None) -> None:
        """Append a message to history and persist it (best-effort)."""
        self.messages.append(message)
        self._persist(message, response)

    def clear(self) -> None:
        """Reset to just the system prompt and clear persisted history."""
        system_msg = (
            self.messages[0]
            if self.messages and self.messages[0].get("role") == "system"
            else None
        )
        if system_msg:
            self.messages = [system_msg]
        else:
            self.messages = [{"role": "system", "content": ""}]

        if self._session_store is not None:
            try:
                self._session_store.clear(
                    keep_system_prompt=self.messages[0].get("content", "")
                )
            except Exception as exc:
                logger.warning("Session clear failed: %s", exc)

    def get_messages(self) -> list[dict[str, Any]]:
        return list(self.messages)

    def count(self) -> int:
        if self._session_store is not None:
            try:
                return self._session_store.count()
            except Exception:
                return len(self.messages)
        return len(self.messages)

    # ---------------------------------------------------------------- persist

    def _persist(self, message: dict[str, Any], response: Any | None = None) -> None:
        """Best-effort persist a message + usage (never raises)."""
        if self._session_store is None:
            return
        try:
            model = None
            prompt_tokens = completion_tokens = total_tokens = None

            if response is not None:
                try:
                    model = getattr(response, "model", None) or self._default_model
                except Exception:
                    model = self._default_model
                usage = getattr(response, "usage", None)
                if usage is not None:
                    prompt_tokens = getattr(usage, "prompt_tokens", None)
                    completion_tokens = getattr(usage, "completion_tokens", None)
                    total_tokens = getattr(usage, "total_tokens", None)
                    if isinstance(usage, dict):
                        prompt_tokens = usage.get("prompt_tokens")
                        completion_tokens = usage.get("completion_tokens")
                        total_tokens = usage.get("total_tokens")
                elif isinstance(response, dict) and "usage" in response:
                    u = response["usage"]
                    if isinstance(u, dict):
                        prompt_tokens = u.get("prompt_tokens")
                        completion_tokens = u.get("completion_tokens")
                        total_tokens = u.get("total_tokens")

            # Assistant messages should carry model even without usage
            if message.get("role") == "assistant" and model is None:
                model = self._default_model

            self._session_store.append(
                message,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )
        except Exception as exc:
            logger.debug("Persist failed: %s", exc)
