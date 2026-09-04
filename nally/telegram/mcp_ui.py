"""MCP Telegram UI — clean provider management.

Handles: /mcp command rendering, connect/disconnect/status for
GitHub, Gmail, and Notion via IntegrationManager.

Telegram presentation concerns only. Business logic is in
nally.integrations.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Provider display order and labels
_PROVIDERS = [
    ("github", "GitHub"),
    ("gmail", "Gmail"),
    ("notion", "Notion"),
]


def _provider_label(provider: str) -> str:
    """Return display label for a provider."""
    for name, label in _PROVIDERS:
        if name == provider:
            return label
    return provider.title()


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------


def build_mcp_keyboard() -> Any:
    """Build inline keyboard for /mcp overview."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    buttons = []
    for provider, label in _PROVIDERS:
        buttons.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"mcp_detail_{provider}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton("Disconnect All", callback_data="mcp_disconnect_all")])
    return InlineKeyboardMarkup(buttons)


def build_provider_keyboard(provider: str, connected: bool) -> Any:
    """Build inline keyboard for a provider detail page."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    if connected:
        btn = InlineKeyboardButton("Disconnect", callback_data=f"mcp_disconnect_{provider}")
    else:
        btn = InlineKeyboardButton("Connect", callback_data=f"mcp_connect_{provider}")
    back = InlineKeyboardButton("Back", callback_data="mcp_back")
    return InlineKeyboardMarkup([[btn], [back]])


# ---------------------------------------------------------------------------
# Status text builders
# ---------------------------------------------------------------------------


def mcp_status_text(user_id: str) -> str:
    """Build the status text for /mcp overview."""
    from nally.integrations import IntegrationManager

    manager = IntegrationManager()
    status = manager.status(user_id)

    # Keep legacy phrase for tests/UX — indicates MCP is available
    # Always include for backward compat with tests expecting "MCP enabled"
    lines = ["MCP enabled", "", "<b>Your integrations</b>", ""]
    for provider, label in _PROVIDERS:
        info = status.get(provider, {})
        connected = info.get("connected", False)
        account = info.get("account")
        icon = "\U0001f7e2" if connected else "\U0001f534"
        if connected and account:
            lines.append(f"{icon} {label}     {html.escape(account)}")
        elif connected:
            lines.append(f"{icon} {label}     Connected")
        else:
            lines.append(f"{icon} {label}     Not connected")

    return "\n".join(lines)


def provider_status_text(user_id: str, provider: str) -> str:
    """Build status text for a provider detail page."""
    from nally.integrations import IntegrationManager

    manager = IntegrationManager()
    connected = manager.is_connected(user_id, provider)
    label = _provider_label(provider)

    icon = "\U0001f7e2" if connected else "\U0001f534"
    status = "Connected" if connected else "Not connected"

    lines = [
        f"<b>{label}</b>",
        "",
        f"Status: {icon} {status}",
    ]

    if connected:
        account = manager.get_provider(provider).get_account_info(user_id)
        if account:
            lines.append(f"Account: {html.escape(account)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Provider connect handlers
# ---------------------------------------------------------------------------


async def handle_provider_connect(
    query: Any, context: Any, provider: str, user_id: str, chat_id: int | None = None
) -> None:
    """Start OAuth flow for a provider.

    Tries canonical OAuthManager (browser flow) first, falls back to
    IntegrationManager (legacy device flow) for backward compat.
    """
    label = _provider_label(provider)

    # Try canonical browser OAuth first
    try:
        from nally.oauth.callback import build_callback_url
        from nally.oauth.manager import get_oauth_manager

        oauth_mgr = get_oauth_manager()
        redirect_uri = build_callback_url(provider)
        session = await oauth_mgr.begin(user_id, provider, redirect_uri)

        # Browser flow: show authorization button
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Authorize", url=session.authorization_url)]]
        )
        status_msg = await query.edit_message_text(
            f"<b>{label} OAuth</b>\n\n"
            f"Click the button to authorize {label}.\n"
            f"You'll be redirected back after approving. I'll check automatically...",
            parse_mode="HTML",
            reply_markup=kb,
        )

        # Poll for credential via TokenStore (callback will store it)
        async def _poll_browser():
            try:
                # Wait up to 5 minutes for user to complete browser flow
                import time

                start = time.time()
                timeout = 300
                while time.time() - start < timeout:
                    if oauth_mgr.has_credential(user_id, provider):
                        if chat_id is not None:
                            try:
                                from nally.telegram.bot import _runtime

                                _runtime.clear_agent(chat_id)
                            except Exception:
                                pass
                        try:
                            kb2 = build_mcp_keyboard()
                            text2 = mcp_status_text(user_id)
                            if status_msg is not None:
                                await status_msg.edit_text(
                                    text2, reply_markup=kb2, parse_mode="HTML"
                                )
                        except Exception:
                            pass
                        return
                    await asyncio.sleep(2)
                # Timeout
                try:
                    if status_msg is not None:
                        await status_msg.edit_text(f"{label} auth timed out. Please try again.")
                except Exception:
                    pass
            except Exception as exc:
                try:
                    if status_msg is not None:
                        await status_msg.edit_text(f"{label} auth failed: {exc}")
                except Exception:
                    pass

        _task = asyncio.create_task(_poll_browser())
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
            logger.debug("mcp_ui: failed to track browser OAuth task")
        return
    except Exception as exc:
        # If not configured, show that directly (don't fallback to device flow)
        if "not configured" in str(exc).lower():
            with contextlib.suppress(Exception):
                await query.edit_message_text(f"Could not start {label} auth: {exc}")
            return
        logger.debug(
            "Browser OAuth failed for %s: %s, falling back to IntegrationManager", provider, exc
        )

    # Fallback: legacy IntegrationManager (device flow)
    from nally.integrations import IntegrationManager

    manager = IntegrationManager()

    try:
        flow_data = await manager.connect(user_id, provider)
    except Exception as exc:
        with contextlib.suppress(Exception):
            await query.edit_message_text(f"Could not start {label} auth: {exc}")
        return

    # Build connect message based on flow type
    if "user_code" in flow_data:
        # Device flow (GitHub, Gmail)
        user_code = flow_data["user_code"]
        verification_uri = flow_data.get("verification_uri", "")
        expires_in = flow_data.get("expires_in", 900)

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open", url=verification_uri)]])
        status_msg = await query.edit_message_text(
            f"<b>{label} OAuth</b>\n\n"
            f"Visit: {html.escape(verification_uri)}\n"
            f"Enter code: <code>{html.escape(user_code)}</code>\n\n"
            f"Code expires in {expires_in // 60} min. I'll check automatically...",
            parse_mode="HTML",
            reply_markup=kb,
        )
    elif "auth_url" in flow_data:
        # PKCE flow (Notion)
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open", url=flow_data["auth_url"])]])
        status_msg = await query.edit_message_text(
            f"<b>{label} OAuth</b>\n\n"
            f"Click the button to authorize.\n"
            f"You'll be redirected back after approving. I'll check automatically...",
            parse_mode="HTML",
            reply_markup=kb,
        )
    else:
        with contextlib.suppress(Exception):
            await query.edit_message_text(f"Unexpected flow response from {label}.")
        return

    # Spawn background polling task
    async def _poll_and_update():
        try:
            connected = await manager.poll_connection(user_id, provider, flow_data)
            if connected:
                # Clear cached agent so next message loads MCP tools with new token
                if chat_id is not None:
                    try:
                        from nally.telegram.bot import _runtime

                        _runtime.clear_agent(chat_id)
                    except Exception:
                        pass
                try:
                    kb2 = build_mcp_keyboard()
                    text2 = mcp_status_text(user_id)
                    if status_msg is not None:
                        await status_msg.edit_text(text2, reply_markup=kb2, parse_mode="HTML")
                except Exception:
                    pass
            else:
                try:
                    if status_msg is not None:
                        await status_msg.edit_text(
                            f"{label} auth failed: connection not established."
                        )
                except Exception:
                    pass
        except Exception as exc:
            try:
                if status_msg is not None:
                    await status_msg.edit_text(f"{label} auth failed: {exc}")
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


# ---------------------------------------------------------------------------
# Provider disconnect handler
# ---------------------------------------------------------------------------


async def handle_provider_disconnect(
    query: Any, provider: str, user_id: str, chat_id: int | None = None
) -> None:
    """Disconnect a provider."""
    from nally.integrations import IntegrationManager

    manager = IntegrationManager()

    try:
        disconnected = manager.disconnect(user_id, provider)
        if disconnected:
            if chat_id is not None:
                try:
                    from nally.telegram.bot import _runtime

                    _runtime.clear_agent(chat_id)
                except Exception:
                    pass
            kb = build_mcp_keyboard()
            text = mcp_status_text(user_id)
            with contextlib.suppress(Exception):
                await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            with contextlib.suppress(Exception):
                await query.answer("Not connected.")
    except Exception as exc:
        with contextlib.suppress(Exception):
            await query.answer(f"Disconnect failed: {exc}")


# ---------------------------------------------------------------------------
# Disconnect all handler
# ---------------------------------------------------------------------------


async def handle_disconnect_all(query: Any, user_id: str, chat_id: int | None = None) -> None:
    """Disconnect all providers."""
    from nally.integrations import IntegrationManager

    manager = IntegrationManager()

    try:
        count = manager.disconnect_all(user_id)
        if count > 0 and chat_id is not None:
            try:
                from nally.telegram.bot import _runtime

                _runtime.clear_agent(chat_id)
            except Exception:
                pass
        kb = build_mcp_keyboard()
        text = mcp_status_text(user_id)
        with contextlib.suppress(Exception):
            if count > 0:
                await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            else:
                await query.answer("No connections to clear.")
    except Exception as exc:
        with contextlib.suppress(Exception):
            await query.answer(f"Disconnect failed: {exc}")


# ---------------------------------------------------------------------------
# Provider detail page
# ---------------------------------------------------------------------------


async def handle_provider_detail(query: Any, provider: str, user_id: str) -> None:
    """Show provider detail page."""
    from nally.integrations import IntegrationManager

    manager = IntegrationManager()
    connected = manager.is_connected(user_id, provider)
    text = provider_status_text(user_id, provider)
    kb = build_provider_keyboard(provider, connected)

    with contextlib.suppress(Exception):
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Back to overview
# ---------------------------------------------------------------------------


async def handle_mcp_overview(query: Any, user_id: str) -> None:
    """Return to /mcp overview."""
    text = mcp_status_text(user_id)
    kb = build_mcp_keyboard()
    with contextlib.suppress(Exception):
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
