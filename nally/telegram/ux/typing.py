"""Telegram typing indicator loop."""

from __future__ import annotations

import asyncio
import contextlib


async def typing_loop(bot, chat_id: int, stop_event: asyncio.Event) -> None:
    """Re-send typing every 4s until stop_event is set."""
    try:
        from telegram.constants import ChatAction
    except ImportError:
        ChatAction = None
    action = getattr(ChatAction, "TYPING", "typing") if ChatAction else "typing"
    while not stop_event.is_set():
        with contextlib.suppress(Exception):
            await bot.send_chat_action(chat_id=chat_id, action=action)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.0)
            break
        except TimeoutError:
            continue
