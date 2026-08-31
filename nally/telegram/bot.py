"""Telegram bot — thin transport/UI adapter for NALLY.

Delegates all business logic to nally.runtime. This file handles only
Telegram-specific concerns: handlers, formatting, typing indicators,
keyboards, and message delivery.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from typing import Any

from .formatting import split_message, telegram_format
from .mcp_ui import (
    build_mcp_keyboard,
    callback_mcp_connect,
    callback_mcp_disconnect,
    mcp_status_text,
)
from .ux.typing import typing_loop

logger = logging.getLogger(__name__)

# Runtime singleton — owns agent lifecycle, identity resolution, DB access
from nally.runtime import default_runtime as _runtime  # noqa: E402


# ------------------------------------------------------------------ Handlers
async def handle_start(update, context) -> None:
    text = (
        "Hello! I'm <i>Nally</i> — your Simply NALLY assistant.\n\n"
        "Send any message to chat. I'm powered by your shared NEON session.\n\n"
        "Commands:\n"
        "• /link — link your Telegram to Google (shared CLI history)\n"
        "• /unlink — remove the link\n"
        "• /status — show link + session info\n"
        "• /mcp — show MCP status + GitHub auth\n"
        "• /clear — clear history\n"
        "• /help — this message\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def handle_help(update, context) -> None:
    await handle_start(update, context)


async def handle_status(update, context) -> None:
    chat_id = update.effective_chat.id
    telegram_id = str(update.effective_user.id) if update.effective_user else None
    if telegram_id is None:
        await update.message.reply_text("Telegram user: unknown")
        return
    status = _runtime.get_status(telegram_id, chat_id=chat_id)
    lines = [f"DB configured: {status.db_configured}"]
    lines.append(f"Telegram ID: {telegram_id}")
    if not status.is_linked:
        lines.append("Linked: no — send /link to connect Google")
    else:
        lines.append(f"Linked: yes \u2192 {status.email} (Google {status.google_id[:10]}\u2026)")
        lines.append(f"Session: {status.session_id[:8]}\u2026")
        lines.append(f"Messages: {status.message_count}  Tokens: {status.total_tokens}")
        if status.agent_message_count:
            lines.append(f"Chat agent messages: {status.agent_message_count}")
    await update.message.reply_text("\n".join(lines))


async def handle_clear(update, context) -> None:
    chat_id = update.effective_chat.id
    telegram_id = str(update.effective_user.id) if update.effective_user else None
    ok = _runtime.clear_history(chat_id, telegram_user_id=telegram_id)
    if ok:
        await update.message.reply_text("History cleared — system prompt kept.")
    else:
        await update.message.reply_text("No history to clear.")


async def handle_unlink(update, context) -> None:
    telegram_id = str(update.effective_user.id) if update.effective_user else None
    if not telegram_id:
        await update.message.reply_text("Cannot determine your Telegram ID.")
        return
    ok = _runtime.unlink(telegram_id)
    _runtime.clear_agent(update.effective_chat.id)
    if ok:
        await update.message.reply_text(
            "Unlinked — your Telegram is no longer connected to Google. Session data kept on the Google account."
        )
    else:
        await update.message.reply_text("Not linked — nothing to unlink. Send /link to connect.")


async def handle_link(update, context) -> None:
    chat_id = update.effective_chat.id
    telegram_id = str(update.effective_user.id) if update.effective_user else None
    first_name = (
        getattr(update.effective_user, "first_name", None) if update.effective_user else None
    )
    username = getattr(update.effective_user, "username", None) if update.effective_user else None
    if not telegram_id:
        await update.message.reply_text("Cannot determine your Telegram ID.")
        return

    # Check if already linked
    existing = _runtime.get_user_info(telegram_id)
    if existing is not None and existing.get("google_id"):
        await update.message.reply_text(
            f"Already linked as {existing.get('email', '')}. Send /unlink first to switch."
        )
        return

    # Check device flow config
    from nally.auth import validate_device_oauth_config

    errs = validate_device_oauth_config()
    if errs:
        await update.message.reply_text(
            "Device flow not configured:\n" + "\n".join(f"• {e}" for e in errs)
        )
        return

    # Request device code
    try:
        import os

        from nally.auth import device_flow_request_code

        client_id = os.getenv("GOOGLE_DEVICE_CLIENT_ID", "").strip()
        data = device_flow_request_code(client_id=client_id)
        user_code = data["user_code"]
        verification_url = (
            data.get("verification_url")
            or data.get("verification_uri")
            or "https://www.google.com/device"
        )
        interval = int(data.get("interval", 5))
        expires_in = int(data.get("expires_in", 1800))
        device_code = data["device_code"]
    except Exception as exc:
        await update.message.reply_text(f"Could not start linking: {exc}")
        return

    # Send instructions with inline keyboard URL button
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Open Google", url=verification_url)]]
        )
    except ImportError:
        keyboard = None

    instructions = (
        f"To link your Telegram to Google:\n\n"
        f"1. Go to: {verification_url}\n"
        f"2. Enter code: <code>{html.escape(user_code)}</code>\n\n"
        f"Code expires in {expires_in // 60} min. I'll check automatically…"
    )
    try:
        status_msg = await update.message.reply_text(
            instructions, parse_mode="HTML", reply_markup=keyboard
        )
    except Exception:
        status_msg = await update.message.reply_text(
            f"To link: Go to {verification_url} and enter code {user_code}"
        )

    # Background poll
    async def poll_and_link() -> None:
        try:
            from nally.auth import device_flow_poll_token, link_telegram_to_google

            # Poll in thread (blocking requests)
            def _poll():
                return device_flow_poll_token(
                    device_code=device_code,
                    interval=interval,
                    expires_in=expires_in,
                )

            token_data = await asyncio.to_thread(_poll)
            id_token = token_data.get("id_token")
            if not id_token:
                await status_msg.edit_text("Link failed: no ID token returned. Try /link again.")
                return

            # Link (also in thread for DB)
            def _link():
                return link_telegram_to_google(
                    telegram_id=telegram_id,
                    first_name=first_name,
                    username=username,
                    id_token_str=id_token,
                )

            result = await asyncio.to_thread(_link)
            user = result["user"]
            # Reset chat agent so next message uses linked session
            _runtime.clear_agent(chat_id)
            try:
                await status_msg.edit_text(
                    f"Linked as <b>{html.escape(user.get('email', '') or '')}</b> — same session as CLI. Try sending a message!",
                    parse_mode="HTML",
                )
            except Exception:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Linked as {user.get('email', '')} — same session as CLI!",
                )
        except RuntimeError as exc:
            msg = str(exc)
            if "already linked" in msg.lower():
                try:
                    await status_msg.edit_text(msg)
                except Exception:
                    await context.bot.send_message(chat_id=chat_id, text=msg)
            elif "denied" in msg.lower() or "expired" in msg.lower():
                try:
                    await status_msg.edit_text(f"Link cancelled: {msg} — send /link to try again.")
                except Exception:
                    await context.bot.send_message(chat_id=chat_id, text=f"Link cancelled: {msg}")
            else:
                try:
                    await status_msg.edit_text(f"Link failed: {msg}")
                except Exception:
                    await context.bot.send_message(chat_id=chat_id, text=f"Link failed: {msg}")
        except Exception as exc:
            try:
                await status_msg.edit_text(f"Link failed: {type(exc).__name__}: {exc}")
            except Exception:
                await context.bot.send_message(chat_id=chat_id, text=f"Link error: {exc}")

    # Fire and forget (don't block handler)
    _task = asyncio.create_task(poll_and_link())
    # Keep reference to avoid garbage collection (RUF006)
    try:
        context.bot_data.setdefault("_link_tasks", []).append(_task)
        _task.add_done_callback(
            lambda t: (
                context.bot_data["_link_tasks"].remove(t)
                if t in context.bot_data.get("_link_tasks", [])
                else None
            )
        )
    except Exception:
        logger.debug("telegram: failed to track link task")


# ------------------------------------------------------------------ MCP


async def handle_mcp(update, context) -> None:
    telegram_id = str(update.effective_user.id) if update.effective_user else None
    if not telegram_id:
        await update.message.reply_text("Cannot determine your Telegram ID.")
        return

    if not _runtime.is_linked(telegram_id):
        await update.message.reply_text(
            "Not linked — send /link to connect your Telegram to Google first."
        )
        return

    text = mcp_status_text()
    kb = build_mcp_keyboard()
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def handle_callback(update, context) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()

    data = query.data
    if data == "mcp_github_connect":
        await callback_mcp_connect(query, context)
    elif data == "mcp_github_disconnect":
        await callback_mcp_disconnect(query)


async def handle_message(update, context) -> None:
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if not text:
        return
    chat_id = update.effective_chat.id
    telegram_id = str(update.effective_user.id) if update.effective_user else None

    # Check link status
    if telegram_id is not None and not _runtime.is_linked(telegram_id):
        await update.message.reply_text(
            "Not linked — send /link to connect your Telegram to Google (shared CLI history)."
        )
        return

    agent = _runtime.get_agent(chat_id, telegram_user_id=telegram_id)

    # Per-chat lock to avoid concurrent runs
    lock = getattr(context, "_nally_locks", None)
    if lock is None:
        context._nally_locks = {}
        lock = context._nally_locks
    if lock.get(chat_id):
        await update.message.reply_text("Still processing your previous message…")
        return
    lock[chat_id] = True

    # Placeholder + typing loop + live UX status
    placeholder = None
    updater = None
    try:
        placeholder = await update.message.reply_text("Thinking...")
    except Exception:
        placeholder = None

    # Wire live status updater if we have a placeholder to edit
    loop = None
    prev_on_tool: Any | None = None
    if placeholder is not None:
        try:
            loop = asyncio.get_running_loop()
            from .ux import StatusUpdater

            updater = StatusUpdater(
                bot=context.bot,
                chat_id=chat_id,
                message_id=placeholder.message_id,
                loop=loop,
            )
            prev_on_tool = getattr(agent, "on_tool_start", None)
            agent.on_tool_start = updater.on_tool_start  # type: ignore[method-assign]
        except Exception:
            logger.debug("telegram: failed to set up status updater")
            updater = None
            loop = None

    stop_event = asyncio.Event()
    typing_task = asyncio.create_task(typing_loop(context.bot, chat_id, stop_event))
    try:
        # Agent.run is sync — run in thread (updater edits happen from that thread)
        reply: str = await asyncio.to_thread(agent.run, text)
        if not reply or not reply.strip():
            reply = "(no reply)"

        # Restore previous callback before sending final answer
        if updater is not None:
            with contextlib.suppress(Exception):
                agent.on_tool_start = prev_on_tool  # type: ignore[method-assign]

        # Convert LLM markdown to Telegram HTML, then split (ensures HTML stays <4096)
        formatted = telegram_format(reply)
        chunks = split_message(formatted)
        # Fallback plain chunks if HTML fails
        plain_chunks = split_message(reply)

        # Edit single status message with final answer, send rest as new messages
        if placeholder is not None:
            if updater is not None:
                try:
                    await updater.finish(chunks[0], parse_mode="HTML")
                except Exception:
                    # HTML failed — fallback to plain
                    with contextlib.suppress(Exception):
                        await placeholder.edit_text(plain_chunks[0])
                    if not plain_chunks:
                        with contextlib.suppress(Exception):
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=plain_chunks[0] if plain_chunks else reply[:4000],
                            )
            else:
                try:
                    await placeholder.edit_text(chunks[0], parse_mode="HTML")
                except Exception:
                    try:
                        await placeholder.edit_text(plain_chunks[0])
                    except Exception:
                        await context.bot.send_message(chat_id=chat_id, text=plain_chunks[0])
            for idx, chunk in enumerate(chunks[1:]):
                await asyncio.sleep(0.6)
                try:
                    await context.bot.send_message(chat_id=chat_id, text=chunk, parse_mode="HTML")
                except Exception:
                    fallback = plain_chunks[idx + 1] if idx + 1 < len(plain_chunks) else chunk
                    await context.bot.send_message(chat_id=chat_id, text=fallback)
        else:
            for idx, chunk in enumerate(chunks):
                try:
                    await context.bot.send_message(chat_id=chat_id, text=chunk, parse_mode="HTML")
                except Exception:
                    fallback = plain_chunks[idx] if idx < len(plain_chunks) else chunk
                    await context.bot.send_message(chat_id=chat_id, text=fallback)
                await asyncio.sleep(0.3)
    except Exception as exc:
        # Restore callback on error too
        if updater is not None:
            with contextlib.suppress(Exception):
                agent.on_tool_start = prev_on_tool  # type: ignore[method-assign]
        err = f"Error: {type(exc).__name__}: {exc}"
        if placeholder is not None:
            if updater is not None:
                with contextlib.suppress(Exception):
                    await updater.finish(err[:4000], parse_mode=None)
                    err = ""  # already sent via edit
            if err:
                try:
                    await placeholder.edit_text(err[:4000])
                except Exception:
                    await context.bot.send_message(chat_id=chat_id, text=err[:4000])
        else:
            await context.bot.send_message(chat_id=chat_id, text=err[:4000])
    finally:
        # Ensure callback restored even if typing cleanup fails
        if updater is not None:
            with contextlib.suppress(Exception):
                if getattr(agent, "on_tool_start", None) is updater.on_tool_start:
                    agent.on_tool_start = prev_on_tool  # type: ignore[method-assign]
        stop_event.set()
        try:
            await asyncio.wait_for(typing_task, timeout=2.0)
        except Exception:
            typing_task.cancel()
        lock[chat_id] = False


# ------------------------------------------------------------------ runner
def run_bot(token: str | None = None, *, drop_pending_updates: bool = False) -> None:
    """Start the Telegram bot (blocking). Requires TELEGRAM_BOT_TOKEN or token arg."""
    import os

    tok = (token or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()).strip()
    if not tok:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set. Get it from @BotFather and set in .env")
    try:
        from telegram.ext import (
            Application,
            CallbackQueryHandler,
            CommandHandler,
            MessageHandler,
            filters,
        )
    except ImportError as exc:
        raise RuntimeError(
            "python-telegram-bot not installed. Run: pip install python-telegram-bot"
        ) from exc

    app = Application.builder().token(tok).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("help", handle_help))
    app.add_handler(CommandHandler("link", handle_link))
    app.add_handler(CommandHandler("unlink", handle_unlink))
    app.add_handler(CommandHandler("status", handle_status))
    app.add_handler(CommandHandler("clear", handle_clear))
    app.add_handler(CommandHandler("mcp", handle_mcp))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    bot_mode = os.getenv("BOT_MODE", "polling").strip().lower()

    if bot_mode == "webhook":
        port = int(os.getenv("PORT", "10000"))
        base_url = os.getenv("WEBHOOK_BASE_URL", "").strip()
        if not base_url:
            raise RuntimeError(
                "WEBHOOK_BASE_URL not set for webhook mode. "
                "Set it to your public URL (e.g. https://simply-nally.onrender.com)"
            )
        # url_path=tok uses the bot token as a secret URL path, blocking random callers
        logger.info("Telegram bot starting (webhook) on port %d…", port)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=tok,
            webhook_url=f"{base_url}/{tok}",
            drop_pending_updates=drop_pending_updates,
            allowed_updates=["message", "callback_query"],
        )
    else:
        logger.info("Telegram bot starting (polling)…")
        app.run_polling(
            drop_pending_updates=drop_pending_updates,
            allowed_updates=["message", "callback_query"],
        )
