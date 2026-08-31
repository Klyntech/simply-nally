#!/usr/bin/env python3
"""Simply NALLY — CLI entry point."""

from __future__ import annotations

import argparse
import sys

from nally.agent import Agent
from nally.config import validate_config

# Windows cp1252 cannot encode emojis — force UTF-8 with replacement
try:
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def build_chat_parser() -> argparse.ArgumentParser:
    """Parser for chat mode (default). Supports prompt + --model etc."""
    p = argparse.ArgumentParser(
        description="Simply NALLY — the smallest reliable agent we can completely understand.",
    )
    p.add_argument(
        "prompt",
        nargs="*",
        help="User message. If omitted, enters interactive mode.",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Override model (default from NALLY_MODEL / provider default)",
    )
    p.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Override max iterations per turn",
    )
    p.add_argument(
        "--no-persist",
        action="store_true",
        help="Disable NEON persistence even if logged in (in-memory only)",
    )
    return p


def build_parser() -> argparse.ArgumentParser:
    """Parser for subcommands (auth, history, clear, telegram)."""
    p = argparse.ArgumentParser(
        description="Simply NALLY — the smallest reliable agent we can completely understand.",
        prog="main.py",
    )
    sub = p.add_subparsers(dest="command")

    # ---- auth subcommands ----
    auth_p = sub.add_parser("auth", help="Authentication (Google OAuth, NEON session)")
    auth_sub = auth_p.add_subparsers(dest="auth_command")
    auth_sub.add_parser("login", help="Login with Google (opens browser)")
    auth_sub.add_parser("logout", help="Logout and clear local session")
    auth_sub.add_parser("status", help="Show current login and session status")
    auth_sub.add_parser("init-db", help="Initialize NEON Postgres schema")

    # ---- session/history ----
    hist_p = sub.add_parser("history", help="Show persisted conversation history")
    hist_p.add_argument("--json", action="store_true", help="Output as JSON")
    hist_p.add_argument("--limit", type=int, default=20, help="Max messages to show (default 20)")
    sub.add_parser("clear", help="Clear persisted history (keeps system prompt)")

    # ---- telegram ----
    tg_p = sub.add_parser("telegram", help="Run Telegram bot (polling)")
    tg_p.add_argument("--token", default=None, help="Override TELEGRAM_BOT_TOKEN")
    tg_p.add_argument("--drop-pending", action="store_true", help="Drop pending updates on start")

    # Also include chat flags so `main.py auth --help` doesn't confuse, and top-level --help lists them
    p.add_argument("--model", default=None, help=argparse.SUPPRESS)
    p.add_argument("--max-iterations", type=int, default=None, help=argparse.SUPPRESS)
    p.add_argument("--no-persist", action="store_true", help=argparse.SUPPRESS)
    return p


def run_once(agent: Agent, prompt: str) -> int:
    print(f"\n> {prompt}\n")
    try:
        reply = agent.run(prompt)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    print(reply)
    return 0


def interactive_loop(agent: Agent) -> int:
    print("Simply NALLY — interactive mode. Type 'exit' or 'quit' to leave.\n")
    # Show persistence status
    if agent.session_store is not None:
        try:
            from nally.auth import get_current_auth

            auth = get_current_auth()
            if auth:
                print(
                    f"(persisting as {auth.get('email', '')} — NEON session {agent.session_store.session_id[:8]}…)\n"
                )
        except Exception:
            pass
    else:
        print("(in-memory mode — run 'python main.py auth login' to persist to NEON)\n")
    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return 0
        if not user:
            continue
        if user.lower() in ("exit", "quit", "/exit", "/quit"):
            print("Bye.")
            return 0
        if user.lower() in ("clear", "/clear"):
            agent.clear_history()
            print("(history cleared)")
            continue
        if user.lower() == "/history":
            for m in agent.get_history():
                role = m.get("role", "?")
                preview = (m.get("content") or "")[:120].replace("\n", " ")
                if m.get("tool_calls"):
                    preview = f"[{len(m['tool_calls'])} tool calls] {preview}"
                print(f"  {role}: {preview}")
            continue
        if user.lower() in ("/status", "/auth"):
            from nally import db as db_mod
            from nally.auth import get_current_auth

            auth = get_current_auth()
            print(f"  logged_in: {auth is not None}")
            if auth:
                print(f"  user: {auth.get('email')} ({auth.get('user_id', '')[:8]}…)")
                print(f"  session: {auth.get('session_id', '')[:8]}…")
            print(f"  db_configured: {db_mod.is_configured()}")
            if agent.session_store:
                print(
                    f"  store: {agent.session_store.session_id[:8]}… count={agent.session_store.count()}"
                )
            continue
        reply = agent.run(user)
        print(f"\nnally> {reply}\n")


# ------------------------------------------------------------------ auth handlers
def handle_auth(args) -> int:
    cmd = args.auth_command or "status"
    if cmd == "login":
        from nally.auth import login_with_browser, validate_oauth_config

        errs = validate_oauth_config()
        # Filter: only report missing env; allow missing psycopg to be caught at login
        missing_env = [e for e in errs if "GOOGLE_" in e]
        if missing_env:
            for e in missing_env:
                print(f"Config error: {e}", file=sys.stderr)
            print("Hint: set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env", file=sys.stderr)
            return 2
        # Check DB
        from nally import db as db_mod

        if not db_mod.is_configured():
            print("Config error: DATABASE_URL not set (NEON connection string)", file=sys.stderr)
            print("Hint: set DATABASE_URL in .env — get it from console.neon.tech", file=sys.stderr)
            return 2
        try:
            result = login_with_browser(open_browser=True)
            user = result["user"]
            sess = result["session"]
            print(f"Logged in as {user['email']} (user {user['id'][:8]}…)")
            print(f"Session {sess['id'][:8]}… ready — history will persist to NEON.")
            return 0
        except RuntimeError as exc:
            print(f"Login failed: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"Login failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

    elif cmd == "logout":
        from nally.auth import get_current_auth, logout_local

        auth = get_current_auth()
        if auth:
            print(f"Logging out {auth.get('email', '')}…")
        ok = logout_local()
        if ok:
            print("Logged out — local credentials cleared. NEON data kept.")
        else:
            print("Not logged in.")
        return 0

    elif cmd == "status":
        from nally import db as db_mod
        from nally.auth import get_current_auth
        from nally.session import get_session_store

        auth = get_current_auth()
        print(f"logged_in: {bool(auth)}")
        if auth:
            print(f"  email: {auth.get('email')}")
            print(f"  user_id: {auth.get('user_id')}")
            print(f"  google_id: {auth.get('google_id')}")
            print(f"  session_id: {auth.get('session_id')}")
        print(f"db_configured: {db_mod.is_configured()}")
        if db_mod.is_configured():
            try:
                conn = db_mod.connect()
                try:
                    db_mod.init_schema(conn)
                    print("db_schema: ok")
                finally:
                    conn.close()
            except Exception as exc:
                print(f"db_schema: error — {exc}", file=sys.stderr)
        store = get_session_store()
        if store:
            print(f"session_store: {store.session_id}")
            try:
                print(f"  messages: {store.count()}")
                info = store.info()
                if info:
                    print(f"  updated: {info.get('updated_at')}")
                    print(f"  total_tokens: {info.get('total_tokens')}")
            except Exception:
                pass
        else:
            print("session_store: none (not logged in or DB unavailable)")
        return 0

    elif cmd == "init-db":
        from nally import db as db_mod

        if not db_mod.is_configured():
            print("DATABASE_URL not set", file=sys.stderr)
            return 2
        try:
            conn = db_mod.connect()
            try:
                db_mod.init_schema(conn)
                print("Schema initialized.")
            finally:
                conn.close()
            return 0
        except Exception as exc:
            print(f"init-db failed: {exc}", file=sys.stderr)
            return 1
    return 2


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Dispatch: if first arg is a known subcommand, parse as subcommand; else chat mode.
    # This avoids argparse collision where `hello` is mistaken for a subcommand.
    if argv and argv[0] in ("auth", "history", "clear", "telegram"):
        parser = build_parser()
        args = parser.parse_args(argv)
        if args.command == "auth":
            return handle_auth(args)
        if args.command == "history":
            import json as _json

            from nally.session import get_session_store

            store = get_session_store()
            if store is None:
                print("No persisted session (not logged in or DB not configured).")
                print("Run: python main.py auth login")
                return 0
            msgs = store.load_with_meta()
            if args.json:
                print(_json.dumps(msgs, indent=2, default=str))
            else:
                print(f"Session {store.session_id} — {len(msgs)} messages\n")
                for m in msgs[-args.limit :]:
                    role = m.get("role", "?")
                    seq = m.get("seq", "?")
                    preview = (m.get("content") or "")[:200].replace("\n", "\\n")
                    if m.get("tool_calls"):
                        preview = f"[{len(m['tool_calls'])} tools] {preview}"
                    meta = []
                    if m.get("model"):
                        meta.append(f"model={m['model']}")
                    if m.get("total_tokens"):
                        meta.append(f"tok={m['total_tokens']}")
                    meta_s = f" ({', '.join(meta)})" if meta else ""
                    print(f"  [{seq}] {role}{meta_s}: {preview}")
                    if m.get("tool_call_id"):
                        print(f"       tool_call_id={m['tool_call_id']}")
            return 0
        if args.command == "clear":
            from nally.session import get_session_store

            store = get_session_store()
            if store is None:
                print("No persisted session to clear.")
                return 0
            from nally.config import get_system_prompt

            count = store.count()
            store.clear(keep_system_prompt=get_system_prompt())
            print(f"Cleared {count} messages. System prompt kept.")
            return 0
        if args.command == "telegram":
            # Requires TELEGRAM_BOT_TOKEN + DB (for linking). Does NOT require LLM key at startup (checked inside bot).
            from nally.config import TELEGRAM_BOT_TOKEN

            token = args.token or TELEGRAM_BOT_TOKEN
            if not token:
                print("Config error: TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
                print(
                    "Hint: get a token from @BotFather and set TELEGRAM_BOT_TOKEN in .env",
                    file=sys.stderr,
                )
                return 2
            # Ensure DB schema is at least initialized
            try:
                from nally import db as db_mod

                if db_mod.is_configured():
                    conn = db_mod.connect()
                    try:
                        db_mod.init_schema(conn)
                    finally:
                        conn.close()
            except Exception as exc:
                print(f"Warning: could not init DB schema: {exc}", file=sys.stderr)
            # LLM key is needed for chatting — validate now
            errs = validate_config(require_api_key=True)
            if errs:
                for e in errs:
                    print(f"Config error: {e}", file=sys.stderr)
                return 2
            try:
                from nally.telegram.bot import run_bot

                run_bot(token=token, drop_pending_updates=bool(args.drop_pending))
                return 0
            except RuntimeError as exc:
                print(f"Telegram bot error: {exc}", file=sys.stderr)
                return 1
            except KeyboardInterrupt:
                print("\nTelegram bot stopped.")
                return 0
        # Fallback (should not happen)
        parser.print_help()
        return 2

    # ---- chat mode ----
    # Handle --help explicitly to show chat help + subcommand hint
    if argv and argv[0] in ("-h", "--help"):
        chat_p = build_chat_parser()
        chat_p.print_help()
        print("\nSubcommands:")
        print("  auth login|logout|status|init-db   Google OAuth + NEON")
        print("  history [--json] [--limit N]        Show persisted history")
        print("  clear                               Clear persisted history")
        print("  telegram [--token TOKEN]            Run Telegram bot (polling)")
        return 0

    chat_parser = build_chat_parser()
    args = chat_parser.parse_args(argv)

    errors = validate_config(require_api_key=True)
    if errors:
        for e in errors:
            print(f"Config error: {e}", file=sys.stderr)
        print("Hint: copy .env.example to .env and set your API key.", file=sys.stderr)
        return 2

    kwargs: dict = {}
    if args.model:
        from nally.llm import LLMClient

        kwargs["llm_client"] = LLMClient(model=args.model)
    if args.max_iterations:
        kwargs["max_iterations"] = args.max_iterations
    if getattr(args, "no_persist", False):
        kwargs["session_store"] = None
        kwargs["auto_persist"] = False

    agent = Agent(**kwargs)

    if args.prompt:
        prompt = " ".join(args.prompt)
        return run_once(agent, prompt)
    else:
        return interactive_loop(agent)


if __name__ == "__main__":
    raise SystemExit(main())
