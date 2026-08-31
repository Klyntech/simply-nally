"""MCP-specific Telegram UI helpers.

Handles: /mcp command rendering, GitHub/Notion OAuth inline keyboard and flows.
These are Telegram presentation concerns -- business logic is delegated to
nally.github_oauth, nally.notion_oauth, and nally.runtime.
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
    nt_auth = default_runtime.is_notion_authenticated()

    gh_btn = InlineKeyboardButton(
        "GitHub: Authenticated" if gh_auth else "GitHub: Not Authenticated",
        callback_data="mcp_github_disconnect" if gh_auth else "mcp_github_connect",
    )
    nt_btn = InlineKeyboardButton(
        "Notion: Authenticated" if nt_auth else "Notion: Not Authenticated",
        callback_data="mcp_notion_disconnect" if nt_auth else "mcp_notion_connect",
    )
    return InlineKeyboardMarkup([[gh_btn], [nt_btn]])


def mcp_status_text() -> str:
    """Build the status text for /mcp."""
    from nally.runtime import default_runtime

    status = default_runtime.get_mcp_status()
    lines = [
        f"MCP enabled: {status['mcp_enabled']}",
        f"mcp package: {'installed' if status['mcp_package_installed'] else 'not installed'}",
        f"GitHub auth: {'yes' if status['github_authenticated'] else 'no'}",
        f"Notion auth: {'yes' if status.get('notion_authenticated') else 'no'}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GitHub OAuth
# ---------------------------------------------------------------------------
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
            f"GitHub OAuth: visit {verification_uri} and enter code: "
            f"<code>{html.escape(user_code)}</code>\n\n"
            f"Code expires in {expires_in // 60} min. I'll check automatically...",
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


# ---------------------------------------------------------------------------
# Notion OAuth
# ---------------------------------------------------------------------------
async def callback_notion_connect(query: Any, context: Any) -> None:
    """Start Notion OAuth PKCE flow from inline button."""
    try:
        from nally.tools.mcp.adapter import _has_mcp

        if not _has_mcp():
            with contextlib.suppress(Exception):
                await query.edit_message_text("mcp package not installed. Run: pip install mcp")
            return
    except Exception:
        logger.debug("mcp_ui: failed to check mcp package availability")

    try:
        from nally.notion_oauth import (
            _get_redirect_uri,
            _pkce_challenge,
            _state,
            build_auth_url,
            discover_oauth_metadata,
            register_client,
        )

        # Discover endpoints
        with contextlib.suppress(Exception):
            await query.edit_message_text("Notion OAuth: discovering endpoints...")

        discover_oauth_metadata()
        redirect_uri = _get_redirect_uri()

        # Register client or use env var
        client_id = os.getenv("NOTION_CLIENT_ID", "").strip()
        client_secret = os.getenv("NOTION_CLIENT_SECRET", "").strip()

        if not client_id:
            from nally.notion_oauth import _REGISTRATION_ENDPOINT

            if not _REGISTRATION_ENDPOINT:
                with contextlib.suppress(Exception):
                    await query.edit_message_text(
                        "Notion OAuth not configured. Set NOTION_CLIENT_ID "
                        "or ensure dynamic registration is available."
                    )
                return
            creds = register_client(redirect_uri)
            client_id = creds["client_id"]
            client_secret = creds.get("client_secret")

        # Generate PKCE params
        verifier, challenge = _pkce_challenge()
        st = _state()

        auth_url = build_auth_url(
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=challenge,
            state=st,
        )

        # Store state for the background task
        context.bot_data["_notion_oauth"] = {
            "verifier": verifier,
            "state": st,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        }

        # Start callback server in background thread BEFORE sending auth URL
        import threading
        from http.server import HTTPServer

        from nally.notion_oauth import CALLBACK_PORT, _CallbackHandler

        _CallbackHandler.code = None
        _CallbackHandler.state = None
        _CallbackHandler.error = None

        _server = HTTPServer(("0.0.0.0", CALLBACK_PORT), _CallbackHandler)
        _server_thread = threading.Thread(target=_server.serve_forever, daemon=True)
        _server_thread.start()
        context.bot_data["_notion_server"] = _server
        logger.info("Notion OAuth: callback server started on port %d", CALLBACK_PORT)

    except Exception as exc:
        with contextlib.suppress(Exception):
            await query.edit_message_text(f"Could not start Notion auth: {exc}")
        return

    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open Notion", url=auth_url)]])
        status_msg = await query.edit_message_text(
            "Notion OAuth: click the button to authorize.\n\n"
            "You'll be redirected back after approving. I'll check automatically...",
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception:
        status_msg = None

    async def _poll_and_update():
        """Poll for the OAuth callback (callback server receives the redirect)."""
        try:
            from nally.notion_oauth import (
                _CallbackHandler,
                _exchange_code,
            )

            # Wait for callback server to receive the code
            timeout = 300  # 5 min
            start = asyncio.get_event_loop().time()

            while asyncio.get_event_loop().time() - start < timeout:
                if _CallbackHandler.code is not None or _CallbackHandler.error is not None:
                    break
                await asyncio.sleep(2)

            oauth_data = context.bot_data.get("_notion_oauth", {})
            code = _CallbackHandler.code
            error = _CallbackHandler.error
            received_state = _CallbackHandler.state

            # Shut down callback server
            server = context.bot_data.pop("_notion_server", None)
            if server:
                server.shutdown()

            if error:
                raise RuntimeError(f"Notion OAuth denied: {error}")
            if not code:
                raise RuntimeError("Notion OAuth timed out -- no code received.")
            if received_state != oauth_data.get("state"):
                raise RuntimeError("Notion OAuth: state mismatch (possible CSRF).")

            # Exchange code for tokens
            _exchange_code(
                code=code,
                code_verifier=oauth_data["verifier"],
                client_id=oauth_data["client_id"],
                redirect_uri=oauth_data["redirect_uri"],
                client_secret=oauth_data.get("client_secret"),
            )

            # Reset callback state for next flow
            _CallbackHandler.code = None
            _CallbackHandler.state = None
            _CallbackHandler.error = None

            # Update UI
            try:
                kb2 = build_mcp_keyboard()
                text2 = mcp_status_text()
                if status_msg is not None:
                    await status_msg.edit_text(text2, reply_markup=kb2)
            except Exception:
                pass

        except Exception as exc:
            # Ensure server is shut down on error too
            server = context.bot_data.pop("_notion_server", None)
            if server:
                server.shutdown()
            try:
                if status_msg is not None:
                    await status_msg.edit_text(f"Notion auth failed: {exc}")
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


async def callback_notion_disconnect(query: Any) -> None:
    """Clear Notion OAuth token."""
    try:
        from nally.notion_oauth import clear_notion_token

        cleared = clear_notion_token()
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
