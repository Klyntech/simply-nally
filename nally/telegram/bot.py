"""Telegram bot — polls, links via Google Device Flow, shared NEON sessions.

Per-chat Agent instances (dict[chat_id, Agent]) with shared SessionStore
when linked. typing loop every 4s, split at 4096, /link via Device Flow.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ utils
TELEGRAM_MAX_LEN = 4096


def split_message(text: str, max_len: int = TELEGRAM_MAX_LEN) -> list[str]:
    """Split text at 4096 code points, preferring newlines > spaces."""
    if not text:
        return [""]
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        # Prefer newline
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = remaining.rfind(" ", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n ")
        if not remaining and not chunks[-1].strip():
            break
    return chunks


def telegram_format(text: str) -> str:
    """Convert LLM markdown to Telegram HTML.

    Handles:
      ```lang\\ncode``` -> <pre>code</pre>
      `inline` -> <code>inline</code>
      **bold** / __bold__ -> <b>bold</b>
      *italic* / _italic_ -> <i>italic</i>
      ~~strike~~ -> <s>strike</s>
      [text](url) -> <a href="url">text</a>
      ### heading -> <b>heading</b>
    Code blocks are protected and HTML-escaped.
    """
    if not text:
        return text

    blocks: dict[str, str] = {}
    inlines: dict[str, str] = {}

    def _save_block(m: re.Match[str]) -> str:
        key = f"\x00TG_BLOCK_{len(blocks)}\x00"
        code = m.group(1) or ""
        # Strip trailing newline for cleaner <pre>
        if code.endswith("\n"):
            code = code[:-1]
        escaped = html.escape(code, quote=False)
        blocks[key] = f"<pre>{escaped}</pre>"
        return key

    def _save_inline(m: re.Match[str]) -> str:
        key = f"\x00TG_INLINE_{len(inlines)}\x00"
        code = m.group(1) or ""
        escaped = html.escape(code, quote=False)
        inlines[key] = f"<code>{escaped}</code>"
        return key

    # Protect code blocks and inline code first
    # Handle ```lang\ncode``` and ```code``` (lang only with newline)
    text = re.sub(r"```(?:\w*\n)?(.*?)```", _save_block, text, flags=re.DOTALL)
    text = re.sub(r"`([^`\n]+?)`", _save_inline, text)

    # Escape remaining HTML (& < >)
    text = html.escape(text, quote=False)

    # Bold: **text** and __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)

    # Italic: *text* (single, not double) and _text_ (word boundaries)
    # After bold, remaining * are single
    text = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", text)

    # Strikethrough
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # Links: [text](url) -> <a>
    # url may contain &amp; from escaping; keep as is (Telegram handles it)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)

    # Headings: ### text -> <b>text</b>
    text = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", text)

    # Restore code
    for key, val in inlines.items():
        text = text.replace(key, val)
    for key, val in blocks.items():
        text = text.replace(key, val)

    return text


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


# ------------------------------------------------------------------ Agent per chat
_agents: dict[int, Any] = {}


def _get_or_create_agent(chat_id: int, telegram_user_id: str | None = None):
    """Return Agent for this chat, linked to shared NEON session if telegram_user is linked."""
    if chat_id in _agents:
        return _agents[chat_id]

    # Try to find linked Google user via telegram_id
    session_store = None
    if telegram_user_id is not None:
        try:
            from nally import db

            if db.is_configured():
                conn = db.connect()
                try:
                    user = db.get_user_by_telegram_id(conn, str(telegram_user_id))
                    if user is not None:
                        sess = db.get_or_create_session(conn, user["id"])
                        from nally.session import SessionStore

                        session_store = SessionStore(session_id=sess["id"])
                finally:
                    conn.close()
        except Exception as exc:
            logger.debug("telegram agent: db lookup failed: %s", exc)
    # Build agent with Conversation (loads history if session_store present)
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
    _agents[chat_id] = agent
    return agent


def _clear_agent(chat_id: int) -> None:
    _agents.pop(chat_id, None)


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
    try:
        from nally import db

        db_ok = db.is_configured()
    except Exception:
        db_ok = False
    lines = [f"DB configured: {db_ok}"]
    if telegram_id is None:
        lines.append("Telegram user: unknown")
        await update.message.reply_text("\n".join(lines))
        return
    lines.append(f"Telegram ID: {telegram_id}")
    try:
        from nally import db as dbmod

        if dbmod.is_configured():
            conn = dbmod.connect()
            try:
                user = dbmod.get_user_by_telegram_id(conn, telegram_id)
                if user is None:
                    lines.append("Linked: no — send /link to connect Google")
                else:
                    lines.append(
                        f"Linked: yes → {user.get('email', '')} (Google {user.get('google_id', '')[:10]}…)"
                    )
                    sess = dbmod.get_or_create_session(conn, user["id"])
                    lines.append(f"Session: {sess['id'][:8]}…")
                    lines.append(
                        f"Messages: {sess.get('message_count', '?')}  Tokens: {sess.get('total_tokens', '?')}"
                    )
                    # Also show chat-local agent state
                    agent = _agents.get(chat_id)
                    if agent is not None:
                        lines.append(f"Chat agent messages: {len(agent.messages)}")
            finally:
                conn.close()
        else:
            lines.append("DB not configured — in-memory only")
    except Exception as exc:
        lines.append(f"Status error: {exc}")
    await update.message.reply_text("\n".join(lines))


async def handle_clear(update, context) -> None:
    chat_id = update.effective_chat.id
    agent = _agents.get(chat_id)
    if agent is not None:
        try:
            agent.clear_history()
            await update.message.reply_text("History cleared — system prompt kept.")
            return
        except Exception as exc:
            await update.message.reply_text(f"Clear failed: {exc}")
            return
    # No agent yet — try to clear DB session directly
    telegram_id = str(update.effective_user.id) if update.effective_user else None
    if telegram_id:
        try:
            from nally import db

            if db.is_configured():
                conn = db.connect()
                try:
                    user = db.get_user_by_telegram_id(conn, telegram_id)
                    if user is not None:
                        sess = db.get_or_create_session(conn, user["id"])
                        from nally.config import get_system_prompt
                        from nally.session import SessionStore

                        store = SessionStore(session_id=sess["id"])
                        store.clear(keep_system_prompt=get_system_prompt())
                        await update.message.reply_text("History cleared — system prompt kept.")
                        return
                finally:
                    conn.close()
        except Exception:
            pass
    await update.message.reply_text("No history to clear.")


async def handle_unlink(update, context) -> None:
    telegram_id = str(update.effective_user.id) if update.effective_user else None
    if not telegram_id:
        await update.message.reply_text("Cannot determine your Telegram ID.")
        return
    try:
        from nally import db

        if not db.is_configured():
            await update.message.reply_text("DB not configured — nothing to unlink.")
            return
        conn = db.connect()
        try:
            ok = db.unlink_telegram(conn, telegram_id)
            _clear_agent(update.effective_chat.id)
            if ok:
                await update.message.reply_text(
                    "Unlinked — your Telegram is no longer connected to Google. Session data kept on the Google account."
                )
            else:
                await update.message.reply_text(
                    "Not linked — nothing to unlink. Send /link to connect."
                )
        finally:
            conn.close()
    except Exception as exc:
        await update.message.reply_text(f"Unlink failed: {exc}")


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
    try:
        from nally import db

        if db.is_configured():
            conn = db.connect()
            try:
                existing = db.get_user_by_telegram_id(conn, telegram_id)
                if existing is not None and existing.get("google_id"):
                    await update.message.reply_text(
                        f"Already linked as {existing.get('email', '')}. Send /unlink first to switch."
                    )
                    return
            finally:
                conn.close()
    except Exception:
        pass

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
            _clear_agent(chat_id)
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
        pass


# ------------------------------------------------------------------ MCP
def _build_mcp_keyboard():
    """Build inline keyboard for /mcp command."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    try:
        from nally.github_oauth import is_github_authenticated

        gh_auth = is_github_authenticated()
    except Exception:
        gh_auth = False

    if gh_auth:
        btn = InlineKeyboardButton(
            "GitHub: Authenticated", callback_data="mcp_github_disconnect"
        )
    else:
        btn = InlineKeyboardButton(
            "GitHub: Not Authenticated", callback_data="mcp_github_connect"
        )
    return InlineKeyboardMarkup([[btn]])


def _mcp_status_text() -> str:
    """Build the status text for /mcp."""
    lines: list[str] = []

    # MCP enabled
    from nally.config import MCP_ENABLED

    lines.append(f"MCP enabled: {MCP_ENABLED}")

    # mcp package
    try:
        from nally.tools.mcp.adapter import _has_mcp

        lines.append(f"mcp package: {'installed' if _has_mcp() else 'not installed'}")
    except Exception:
        lines.append("mcp package: unavailable")

    # GitHub auth
    try:
        from nally.github_oauth import is_github_authenticated

        lines.append(f"GitHub auth: {'yes' if is_github_authenticated() else 'no'}")
    except Exception:
        lines.append("GitHub auth: unknown")

    return "\n".join(lines)


async def handle_mcp(update, context) -> None:
    telegram_id = str(update.effective_user.id) if update.effective_user else None
    if not telegram_id:
        await update.message.reply_text("Cannot determine your Telegram ID.")
        return

    # Only linked users
    try:
        from nally import db

        if db.is_configured():
            conn = db.connect()
            try:
                user = db.get_user_by_telegram_id(conn, telegram_id)
                if user is None:
                    await update.message.reply_text(
                        "Not linked — send /link to connect your Telegram to Google first."
                    )
                    return
            finally:
                conn.close()
    except Exception:
        pass

    text = _mcp_status_text()
    kb = _build_mcp_keyboard()
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def handle_callback(update, context) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()

    data = query.data
    if data == "mcp_github_connect":
        await _callback_mcp_connect(query, context)
    elif data == "mcp_github_disconnect":
        await _callback_mcp_disconnect(query)


async def _callback_mcp_connect(query, context) -> None:
    """Start GitHub OAuth device flow from inline button."""
    # Check config first
    import os

    cid = os.getenv("GITHUB_CLIENT_ID", "").strip()
    csec = os.getenv("GITHUB_CLIENT_SECRET", "").strip()
    if not cid or not csec:
        with contextlib.suppress(Exception):
            await query.edit_message_text(
                "GitHub OAuth not configured. Set GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET."
            )
        return

    # Check mcp package
    try:
        from nally.tools.mcp.adapter import _has_mcp

        if not _has_mcp():
            with contextlib.suppress(Exception):
                await query.edit_message_text(
                    "mcp package not installed. Run: pip install mcp"
                )
            return
    except Exception:
        pass

    # Request device code
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

    # Show instructions
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Open GitHub", url=verification_uri)]]
        )
        status_msg = await query.edit_message_text(
            f"GitHub OAuth: visit {verification_uri} and enter code: <code>{html.escape(user_code)}</code>\n\n"
            f"Code expires in {expires_in // 60} min. I'll check automatically…",
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception:
        status_msg = None

    # Background poll
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
            # Update to authenticated
            try:
                kb2 = _build_mcp_keyboard()
                text2 = _mcp_status_text()
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
        pass


async def _callback_mcp_disconnect(query) -> None:
    """Clear GitHub device-flow token."""
    try:
        from nally.github_oauth import clear_github_token

        cleared = clear_github_token()
        if cleared:
            kb = _build_mcp_keyboard()
            text = _mcp_status_text()
            with contextlib.suppress(Exception):
                await query.edit_message_text(text, reply_markup=kb)
        else:
            with contextlib.suppress(Exception):
                await query.answer("No cached token to clear.")
    except Exception as exc:
        with contextlib.suppress(Exception):
            await query.answer(f"Disconnect failed: {exc}")


async def handle_message(update, context) -> None:
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if not text:
        return
    chat_id = update.effective_chat.id
    telegram_id = str(update.effective_user.id) if update.effective_user else None

    # Check link status
    is_linked = False
    if telegram_id is not None:
        try:
            from nally import db

            if db.is_configured():
                conn = db.connect()
                try:
                    user = db.get_user_by_telegram_id(conn, telegram_id)
                    is_linked = user is not None
                finally:
                    conn.close()
        except Exception:
            is_linked = False
    if not is_linked:
        await update.message.reply_text(
            "Not linked — send /link to connect your Telegram to Google (shared CLI history)."
        )
        return

    agent = _get_or_create_agent(chat_id, telegram_user_id=telegram_id)

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

    # Also handle /clear etc as plain text "/clear" (some clients)
    logger.info("Telegram bot starting (polling)…")
    app.run_polling(
        drop_pending_updates=drop_pending_updates,
        allowed_updates=["message", "callback_query"],
    )
