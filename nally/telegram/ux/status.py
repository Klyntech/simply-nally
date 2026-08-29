"""Live status — single Telegram message edited with friendly tool names.

UX: send "Thinking..." → edit to "Searching the web" → edit to final answer.
Rate-limited to ~2/s to respect Telegram limits. Thread-safe via run_coroutine_threadsafe.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

TOOL_STATUS: dict[str, str] = {
    "web_search": "Searching the web",
    "fetch": "Reading webpage",
    "run_command": "Running command on your computer",
    "read_file": "Reading file",
    "write_file": "Writing file",
    "list_dir": "Listing files",
}


def friendly_status(tool_name: str) -> str:
    """Map tool name to human-friendly status text."""
    return TOOL_STATUS.get(tool_name, f"Using {tool_name}")


class StatusUpdater:
    """Manages one Telegram message edited with status updates.

    Created in the async handler (captures the running event loop).
    on_tool_start is called from Agent's sync thread via run_coroutine_threadsafe.
    finish() is awaited from the async handler after Agent.run() returns.
    """

    def __init__(
        self,
        bot: Any,
        chat_id: int,
        message_id: int,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self._loop = loop or asyncio.get_running_loop()
        self._last_edit: float = float("-inf")

    # --- callback for Agent.on_tool_start (sync, called from thread) ----
    def on_tool_start(self, tool_name: str, args: dict[str, Any] | None = None) -> None:
        """Sync callback — safe to call from Agent thread. Edits status to friendly name."""
        text = friendly_status(tool_name)
        now = time.monotonic()
        # Rate-limit: skip if < 0.5s since last edit (Telegram ~1/s)
        if now - self._last_edit < 0.5:
            return
        self._last_edit = now
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(self._edit(text), self._loop)

    # --- async helpers (called from event loop) -------------------------
    async def _edit(self, text: str) -> None:
        with contextlib.suppress(Exception):
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
            )

    async def update(self, text: str) -> None:
        """Async update from handler (no rate-limit, direct)."""
        self._last_edit = time.monotonic()
        await self._edit(text)

    async def finish(self, text: str) -> None:
        """Replace status message with final answer."""
        await self._edit(text)
