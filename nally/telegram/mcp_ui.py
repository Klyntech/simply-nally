"""MCP-specific Telegram UI helpers.

Handles: /mcp command rendering, GitHub OAuth inline keyboard and device flow.
These are Telegram presentation concerns — business logic is delegated to nally.github_oauth and nally.runtime.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def build_mcp_keyboard() -> Any:
    """Build inline keyboard for /mcp command."""
    from nally.runtime import default_runtime
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    gh_auth = default_runtime.is_github_authenticated()

    if gh_auth:
        btn = InlineKeyboardButton("GitHub: Authenticated", callback_data="mcp_github_disconnect")
    else:
        btn = InlineKeyboardButton("GitHub: Not Authenticated", callback_data="mcp_github_connect")
    return InlineKeyboardMarkup([[btn]])


def mcp_status_text() -> str:
    """Build the status text for /mcp."""
    from nally.runtime import default_runtime

    status = default_runtime.get_mcp_status()
    lines = [
        f"MCP enabled: {status['mcp_enabled']}",
        f"mcp package: {'installed' if status['mcp_package_installed'] else 'not installed'}",
        f"GitHub auth: {'yes' if status['github_authenticated'] else 'no'}",
    ]
    return "\n".join(lines)


async def callback_mcp_connect(query: Any, context: Any) -> None:
    """Start GitHub OAuth device flow from inline button."""
    cid = os.getenv("GITHUB_CLIENT_ID", "").strip()
    csec = os.getenv("GITHUB_CLIENT_SECRET", "").strip()
    if not cid or not csec:
        with contextlib.suppress(Exception):
            await query.edit_message_text(
                "GitHub OAuth not configured. Set GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET."
            )
        return

    try:
        from nally.tools.mcp.adapter import _has_mcp

        if not _has_mcp():
            with contextlib.suppress(Exception):
                await query.edit_message_text("mcp package not installed. Run: pip install mcp")
            return
    except Exception:
        logger.debug("mcp_ui: failed to check mcp package availability")

    try:
        from nally.github_oauth import github_request_device_code

        data = github_request_device_code()
    except Exception as exc:
        with contextlib.suppress(Exception):
            await query.edit_message_text(f"Could not start GitHub auth: {exc}")
        return

    user_code = data["user_code"]
    verification_uri = data.get("verification_uri", "https://github.com/login/device")
    expires_in = data.get("expires_in", 900)
    interval = data.get("interval", 5)
    device_code = data["device_code"]

    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open GitHub", url=verification_uri)]])
        status_msg = await query.edit_message_text(
            f"GitHub OAuth: visit {verification_uri} and enter code: <code>{html.escape(user_code)}</code>\n\n"
            f"Code expires in {expires_in // 60} min. I'll check automatically\u2026",
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception:
        status_msg = None

    async def _poll_and_update():
        try:
            from nally.github_oauth import github_poll_token

            def _poll():
                return github_poll_token(
                    device_code=device_code,
                    expires_in=expires_in,
                    interval=interval,
                )

            await asyncio.to_thread(_poll)
            try:
                kb2 = build_mcp_keyboard()
                text2 = mcp_status_text()
                if status_msg is not None:
                    await status_msg.edit_text(text2, reply_markup=kb2)
            except Exception:
                pass
        except Exception as exc:
            try:
                if status_msg is not None:
                    await status_msg.edit_text(f"GitHub auth failed: {exc}")
            except Exception:
                pass

    _task = asyncio.create_task(_poll_and_update())
    try:
        context.bot_data.setdefault("_mcp_tasks", []).append(_task)
        _task.add_done_callback(
            lambda t: (
                context.bot_data["_mcp_tasks"].remove(t)
                if t in context.bot_data.get("_mcp_tasks", [])
                else None
            )
        )
    except Exception:
        logger.debug("mcp_ui: failed to track background task")


async def callback_mcp_disconnect(query: Any) -> None:
    """Clear GitHub device-flow token."""
    try:
        from nally.github_oauth import clear_github_token

        cleared = clear_github_token()
        if cleared:
            kb = build_mcp_keyboard()
            text = mcp_status_text()
            with contextlib.suppress(Exception):
                await query.edit_message_text(text, reply_markup=kb)
        else:
            with contextlib.suppress(Exception):
                await query.answer("No cached token to clear.")
    except Exception as exc:
        with contextlib.suppress(Exception):
            await query.answer(f"Disconnect failed: {exc}")
