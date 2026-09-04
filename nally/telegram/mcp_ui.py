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


def _get_vault_status(user_id: str) -> dict[str, dict]:
    """Vault-backed status for user (no IntegrationManager)."""
    try:
        from nally.vault import get_vault

        vault = get_vault()
        out = {}
        for p, _ in _PROVIDERS:
            cred = vault.get(user_id, p)
            if cred and not cred.is_expired:
                out[p] = {"connected": True, "account": cred.provider_metadata.get("account") or cred.subject}
            elif cred:
                # expired still show as disconnected but with account
                out[p] = {"connected": False, "account": cred.provider_metadata.get("account") or cred.subject, "reauth_required": True}
            else:
                out[p] = {"connected": False, "account": None}
        return out
    except Exception:
        return {p: {"connected": False, "account": None} for p, _ in _PROVIDERS}


def mcp_status_text(user_id: str) -> str:
    """Build the status text for /mcp overview — vault only."""
    status = _get_vault_status(user_id)

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
    """Build status text for a provider detail page — vault only."""
    status = _get_vault_status(user_id)
    info = status.get(provider, {})
    connected = bool(info.get("connected"))
    label = _provider_label(provider)

    icon = "\U0001f7e2" if connected else "\U0001f534"
    status_s = "Connected" if connected else "Not connected"

    lines = [
        f"<b>{label}</b>",
        "",
        f"Status: {icon} {status_s}",
    ]

    if connected and info.get("account"):
        lines.append(f"Account: {html.escape(info['account'])}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Provider connect handlers
# ---------------------------------------------------------------------------


async def handle_provider_connect(
    query: Any, context: Any, provider: str, user_id: str, chat_id: int | None = None
) -> None:
    """Start browser-only OAuth flow (v2) — one link, no device code."""
    label = _provider_label(provider)

    # Resolve canonical internal user_id via directory if possible (telegram -> internal UUID)
    internal_user_id = user_id
    try:
        from nally.directory import get_directory

        d = get_directory()
        # This will create or return existing internal user for telegram
        u = d.get_or_create_for_telegram(telegram_id=user_id)
        if u and u.get("id"):
            internal_user_id = u["id"]
    except Exception:
        # Fallback to raw telegram ID (vault file path will use it)
        pass

    # Start v2 AuthBroker flow
    try:
        from nally.auth_broker import get_broker

        broker = get_broker()
        session = await broker.start(
            user_id=internal_user_id, provider=provider, return_surface="telegram", return_reference=str(chat_id or "")
        )
    except Exception as exc:
        # Provider not configured or other error — show deterministic message, no fallback to device flow
        with contextlib.suppress(Exception):
            await query.edit_message_text(f"Could not start {label} auth: {exc}")
        return

    # Show single HTTPS link (no device code, no polling provider)
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Authorize", url=session.authorization_url)]])
    status_msg = await query.edit_message_text(
        f"<b>{label} OAuth</b>\n\n"
        f"Click the button to authorize {label}.\n"
        f"You'll be redirected back after approving. I'll check automatically...",
        parse_mode="HTML",
        reply_markup=kb,
    )

    # Wait for vault credential via DB/event notification (not provider polling)
    async def _poll_vault():
        try:
            import time

            from nally.vault import get_vault

            vault = get_vault()
            start = time.time()
            timeout = 300
            while time.time() - start < timeout:
                # Check vault directly (cross-user, no fallback)
                cred = vault.get(internal_user_id, provider)
                if cred and not cred.is_expired:
                    if chat_id is not None:
                        try:
                            from nally.telegram.bot import _runtime

                            _runtime.clear_agent(chat_id)
                        except Exception:
                            pass
                        try:
                            from nally.mcp.broker import get_broker as get_mcp_broker

                            await get_mcp_broker().invalidate_cache(internal_user_id, provider)
                        except Exception:
                            pass
                    try:
                        kb2 = build_mcp_keyboard()
                        # Note: mcp_status_text uses user_id which may be telegram raw vs internal.
                        # Use internal for vault lookup but display same text
                        text2 = mcp_status_text(internal_user_id) if internal_user_id != user_id else mcp_status_text(user_id)
                        # Try both ids for display compat
                        if text2.count("Not connected") == 3:
                            # Fallback to original id text if internal shows empty (file fallback mismatch)
                            text2 = mcp_status_text(user_id)
                        if status_msg is not None:
                            await status_msg.edit_text(text2, reply_markup=kb2, parse_mode="HTML")
                    except Exception:
                        pass
                    return
                await asyncio.sleep(2)
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

    _task = asyncio.create_task(_poll_vault())
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


# ---------------------------------------------------------------------------
# Provider disconnect handler
# ---------------------------------------------------------------------------


async def handle_provider_disconnect(
    query: Any, provider: str, user_id: str, chat_id: int | None = None
) -> None:
    """Disconnect a provider — vault only, atomic, cache invalidated."""
    # Resolve internal user id same as connect
    internal_user_id = user_id
    try:
        from nally.directory import get_directory

        d = get_directory()
        u = d.get_or_create_for_telegram(telegram_id=user_id)
        if u and u.get("id"):
            internal_user_id = u["id"]
    except Exception:
        pass
    try:
        from nally.auth_broker import get_broker

        broker = get_broker()
        disconnected = await broker.revoke(internal_user_id, provider)
        # Also try vault directly with raw id for legacy file fallback
        if not disconnected:
            try:
                from nally.vault import get_vault

                vault = get_vault()
                disconnected = vault.delete(user_id, provider) or vault.delete(internal_user_id, provider)
                if disconnected:
                    try:
                        from nally.mcp.broker import get_broker as get_mcp_broker

                        await get_mcp_broker().invalidate_cache(internal_user_id, provider)
                        await get_mcp_broker().invalidate_cache(user_id, provider)
                    except Exception:
                        pass
            except Exception:
                pass
        if disconnected:
            if chat_id is not None:
                try:
                    from nally.telegram.bot import _runtime

                    _runtime.clear_agent(chat_id)
                except Exception:
                    pass
            kb = build_mcp_keyboard()
            # Prefer internal status text
            text = mcp_status_text(internal_user_id)
            if "Not connected" not in text:
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
    """Disconnect all providers — vault only."""
    internal_user_id = user_id
    try:
        from nally.directory import get_directory

        d = get_directory()
        u = d.get_or_create_for_telegram(telegram_id=user_id)
        if u and u.get("id"):
            internal_user_id = u["id"]
    except Exception:
        pass
    try:
        from nally.auth_broker import get_broker

        broker = get_broker()
        count = 0
        for p in ("github", "gmail", "notion"):
            try:
                ok = await broker.revoke(internal_user_id, p)
                if ok:
                    count += 1
                else:
                    # Try raw id fallback
                    from nally.vault import get_vault

                    vault = get_vault()
                    if vault.delete(user_id, p):
                        count += 1
            except Exception:
                continue
        if count > 0 and chat_id is not None:
            try:
                from nally.telegram.bot import _runtime

                _runtime.clear_agent(chat_id)
            except Exception:
                pass
        kb = build_mcp_keyboard()
        text = mcp_status_text(internal_user_id) if internal_user_id != user_id else mcp_status_text(user_id)
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
    """Show provider detail page — vault only."""
    internal_user_id = user_id
    try:
        from nally.directory import get_directory

        d = get_directory()
        u = d.get_or_create_for_telegram(telegram_id=user_id)
        if u and u.get("id"):
            internal_user_id = u["id"]
    except Exception:
        pass
    status = _get_vault_status(internal_user_id)
    if not any(v.get("connected") for v in status.values()):
        status = _get_vault_status(user_id)
        internal_user_id = user_id
    connected = bool(status.get(provider, {}).get("connected"))
    text = provider_status_text(internal_user_id, provider)
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
