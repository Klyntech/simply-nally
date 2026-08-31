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

    # ---- mcp (permanent GitHub device flow + Gmail) ----
    mcp_p = sub.add_parser("mcp", help="MCP auth — GitHub device flow + Gmail (Google official MCP)")
    mcp_sub = mcp_p.add_subparsers(dest="mcp_command")
    mcp_sub.add_parser("status", help="Show MCP status + GitHub/Gmail auth")
    mcp_sub.add_parser("login", help="Login to GitHub via device flow (permanent)")
    mcp_sub.add_parser("logout", help="Clear cached GitHub MCP token")
    mcp_sub.add_parser("gmail-login", help="Login to Gmail via Google OAuth (for Gmail MCP)")
    mcp_sub.add_parser("gmail-logout", help="Clear cached Gmail MCP token")
    mcp_sub.add_parser("gmail-status", help="Show Gmail MCP status only")

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
    conv = agent.conversation
    if conv.is_persisting:
        try:
            from nally.auth import get_current_auth

            auth = get_current_auth()
            if auth:
                print(
                    f"(persisting as {auth.get('email', '')} — NEON session {conv.session_id[:8]}…)\n"
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
            if conv.is_persisting:
                print(
                    f"  store: {conv.session_id[:8]}… count={conv.count()}"
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
                conn = db_mod.pooled_connect()
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
            conn = db_mod.pooled_connect()
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


def handle_mcp(args) -> int:
    """MCP auth — GitHub device flow + Gmail Google OAuth (no npm/npx)."""
    cmd = getattr(args, "mcp_command", None) or "status"

    if cmd in ("status", "gmail-status"):
        from nally.config import MCP_ENABLED
        from nally.github_oauth import is_github_authenticated
        from nally.integrations.token_store import get_valid_token
        from nally.mcp.adapter import _has_mcp as has_mcp_pkg

        GMAIL_CACHE_FILE = "~/.config/simply-nally/tokens/_global/gmail.json"

        def _gmail_is_connected():
            import os
            env = (os.getenv("GMAIL_TOKEN", "").strip()
                   or os.getenv("GMAIL_OAUTH_TOKEN", "").strip()
                   or os.getenv("GOOGLE_GMAIL_TOKEN", "").strip())
            if env:
                return True
            return get_valid_token("_global", "gmail") is not None

        def _gmail_cached_token():
            return get_valid_token("_global", "gmail")

        if cmd == "gmail-status":
            print(f"Gmail MCP URL: {__import__('nally.config', fromlist=['GMAIL_MCP_URL']).GMAIL_MCP_URL}")
            print(f"Gmail auth: {'yes' if _gmail_is_connected() else 'no'}")
            tok = _gmail_cached_token()
            if tok:
                print(f"cached token: yes ({tok[:12]})")
                print(f"cache: {GMAIL_CACHE_FILE} (0600)")
            else:
                import os as _os
                has_env = bool(_os.getenv("GMAIL_TOKEN", "").strip() or _os.getenv("GMAIL_OAUTH_TOKEN", "").strip())
                if has_env:
                    print("cached token: no (using GMAIL_TOKEN env)")
                else:
                    print("cached token: no (run `python main.py mcp gmail-login` or set GMAIL_TOKEN)")
            try:
                from nally.config import get_mcp_servers_config
                cfg = get_mcp_servers_config()
                gm = cfg.get("gmail") if cfg else None
                if gm:
                    print(f"server gmail: {gm}")
                elif not cfg:
                    print("servers: none (check NALLY_MCP_ENABLED)")
            except Exception as exc:
                print(f"servers error: {exc}")
            return 0

        # full status
        print(f"MCP enabled: {MCP_ENABLED}")
        try:
            status = "installed" if has_mcp_pkg() else 'not installed (pip install "simply-nally[mcp]")'
            print(f"mcp package: {status}")
        except Exception:
            print("mcp package: unknown")
        print(f"GitHub auth: {'yes' if is_github_authenticated() else 'no'}")
        gh_tok = get_valid_token("_global", "github")
        if gh_tok:
            print(f"  cached token: yes ({gh_tok[:12]})")
        else:
            print("  cached token: no (run `python main.py mcp login`)")
        print(f"Gmail auth: {'yes' if _gmail_is_connected() else 'no'}")
        tok = _gmail_cached_token()
        if tok:
            print(f"  cached token: yes ({tok[:12]})")
        else:
            import os as _os
            has_env = bool(_os.getenv("GMAIL_TOKEN", "").strip() or _os.getenv("GMAIL_OAUTH_TOKEN", "").strip())
            if has_env:
                print("  cached token: no (using GMAIL_TOKEN env)")
            else:
                print("  cached token: no (run `python main.py mcp gmail-login` or set GMAIL_TOKEN)")
        try:
            from nally.config import get_mcp_servers_config
            cfg = get_mcp_servers_config()
            if cfg:
                for name, c in cfg.items():
                    print(f"  server {name}: {c}")
            else:
                print("servers: none (check NALLY_MCP_ENABLED)")
        except Exception as exc:
            print(f"servers error: {exc}")
        return 0

    if cmd == "logout":
        from nally.github_oauth import clear_github_token

        if clear_github_token():
            print("Cleared cached GitHub MCP token.")
        else:
            print("No cached token to clear.")
        return 0

    if cmd in ("gmail-logout",):
        from nally.integrations.token_store import clear_token

        if clear_token("_global", "gmail"):
            print("Cleared cached Gmail MCP token.")
            print("Removed ~/.config/simply-nally/tokens/_global/gmail.json")
        else:
            print("No cached Gmail token to clear.")
        return 0

    if cmd == "login":
        import os

        cid = os.getenv("GITHUB_CLIENT_ID", "").strip()
        csec = os.getenv("GITHUB_CLIENT_SECRET", "").strip()
        if not cid or not csec:
            print("Config error: GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET not set", file=sys.stderr)
            print("Hint: create an OAuth App at https://github.com/settings/developers and enable Device Flow", file=sys.stderr)
            return 2
        try:
            from nally.github_oauth import github_poll_token, github_request_device_code

            print("Requesting GitHub device code…")
            data = github_request_device_code()
            uri = data.get("verification_uri", "https://github.com/login/device")
            code = data["user_code"]
            interval = data.get("interval", 5)
            expires = data.get("expires_in", 900)
            print(f"\nGo to: {uri}")
            print(f"Enter code: {code}")
            print(f"Expires in {expires//60} min — polling every {interval}s…\n")
            # Blocking poll (same as Telegram inline flow, permanent)
            token = github_poll_token(
                device_code=data["device_code"],
                expires_in=expires,
                interval=interval,
            )
            print(f"GitHub MCP login permanent — token cached: {token[:12]}…")
            print("Stored at ~/.config/simply-nally/github_oauth_token.json (0600)")
            return 0
        except RuntimeError as exc:
            print(f"Login failed: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\nCancelled.", file=sys.stderr)
            return 130
        except Exception as exc:
            print(f"Login error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

    if cmd == "gmail-login":
        import os
        import time

        # Gmail uses browser (Desktop) flow — Device flow does NOT allow gmail scopes (invalid_scope)
        # Try Gmail-specific OAuth first, then fallback to the main Google Desktop client
        cid = os.getenv("GMAIL_CLIENT_ID", "").strip() or os.getenv("GOOGLE_CLIENT_ID", "").strip()
        csec = os.getenv("GMAIL_CLIENT_SECRET", "").strip() or os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
        scopes = os.getenv("GMAIL_OAUTH_SCOPES", "").strip()
        if not scopes:
            from nally.config import GMAIL_OAUTH_SCOPES as _def_scopes

            scopes = _def_scopes
        scopes_list = [s.strip() for s in scopes.split() if s.strip()]
        # Ensure openid/email are present so we could fetch userinfo if needed
        if "openid" not in scopes_list:
            scopes_list = ["openid", "https://www.googleapis.com/auth/userinfo.email"] + scopes_list
        if not cid or not csec:
            print("Config error: GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET not set", file=sys.stderr)
            print("Hint: create a Google Cloud OAuth client (Desktop app) for Gmail", file=sys.stderr)
            print("  1. Go to https://console.cloud.google.com/apis/credentials", file=sys.stderr)
            print("  2. Create OAuth client → Desktop app (or reuse GOOGLE_CLIENT_ID if same project)", file=sys.stderr)
            print("  3. Enable Gmail API + gmailmcp.googleapis.com in the project", file=sys.stderr)
            print("  4. Add scopes gmail.readonly + gmail.compose to the consent screen", file=sys.stderr)
            print("  5. Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env (or set GOOGLE_CLIENT_ID)", file=sys.stderr)
            print("", file=sys.stderr)
            print("Alternative (no OAuth app): set GMAIL_TOKEN env to a Google OAuth access token", file=sys.stderr)
            print("  with gmail.readonly + gmail.compose scopes (from gcloud auth or OAuth playground).", file=sys.stderr)
            return 2
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
            from nally.integrations.token_store import write_token

            GMAIL_CACHE_FILE = "~/.config/simply-nally/tokens/_global/gmail.json"
            cfg = {
                "installed": {
                    "client_id": cid,
                    "client_secret": csec,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
            flow = InstalledAppFlow.from_client_config(cfg, scopes=scopes_list)
            print("Opening browser for Gmail OAuth (scopes: gmail.readonly, gmail.compose)…")
            print("If browser doesn't open, copy the URL printed below (you have 3 minutes).")
            try:
                creds = flow.run_local_server(
                    port=0,
                    open_browser=True,
                    timeout_seconds=180,
                    prompt="consent",
                    access_type="offline",
                )
            except AttributeError as exc:
                if "'NoneType' object has no attribute 'replace'" in str(exc) or "NoneType" in str(type(exc)):
                    raise RuntimeError(
                        "Browser authorization timed out (no request received for 3 minutes). "
                        "Please run `python main.py mcp gmail-login` again and visit the URL promptly, "
                        "complete Google consent, and allow the localhost redirect."
                    ) from exc
                raise
            access_token = getattr(creds, "token", None)
            if not access_token:
                raise RuntimeError(f"No access token from flow: {creds}")
            expiry = getattr(creds, "expiry", None)
            if expiry is not None:
                try:
                    expires_at = expiry.timestamp()  # type: ignore[union-attr]
                except Exception:
                    expires_at = time.time() + 3600
            else:
                expires_at = time.time() + 3600
            refresh = getattr(creds, "refresh_token", None)
            token_data = {"access_token": access_token, "expires_at": expires_at}
            if refresh:
                token_data["refresh_token"] = refresh
            write_token("_global", "gmail", token_data)
            print(f"Gmail MCP login — token cached: {access_token[:12]}")
            print(f"Stored at {GMAIL_CACHE_FILE} (0600), expires {int((expires_at - time.time())//60)} min")
            if refresh:
                print("Refresh token saved — future sessions can auto-refresh (restart not needed).")
            else:
                print("Note: no refresh token (already consented). Re-run if token expires in 1h.")
            print("Gmail tools will be available after restart (if NALLY_MCP_ENABLED=true).")
            return 0
        except ImportError as exc:
            print(f"Missing dep: {exc}. Run: pip install google-auth google-auth-oauthlib requests", file=sys.stderr)
            return 1
        except RuntimeError as exc:
            print(f"Gmail login failed: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\nCancelled.", file=sys.stderr)
            return 130
        except Exception as exc:
            print(f"Gmail login error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

    return 2


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Dispatch: if first arg is a known subcommand, parse as subcommand; else chat mode.
    # This avoids argparse collision where `hello` is mistaken for a subcommand.
    if argv and argv[0] in ("auth", "history", "clear", "telegram", "mcp"):
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
                    conn = db_mod.pooled_connect()
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
        if args.command == "mcp":
            return handle_mcp(args)
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
        print("  mcp status|login|logout|gmail-login|gmail-logout|gmail-status")
        print("                                     GitHub (device flow) + Gmail (Google MCP, no npm)")
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

    agent_kwargs: dict = {}
    if args.model:
        from nally.llm import LLMClient

        agent_kwargs["llm_client"] = LLMClient(model=args.model)
    if args.max_iterations:
        agent_kwargs["max_iterations"] = args.max_iterations

    # Build conversation (persistence is a Conversation concern, not Agent's)
    from nally.config import get_system_prompt
    from nally.conversation import Conversation

    no_persist = getattr(args, "no_persist", False)
    conv_kwargs: dict = {"system_prompt": get_system_prompt()}
    if no_persist:
        conv_kwargs["auto_persist"] = False
    conversation = Conversation(**conv_kwargs)
    agent_kwargs["conversation"] = conversation

    agent = Agent(**agent_kwargs)

    if args.prompt:
        prompt = " ".join(args.prompt)
        return run_once(agent, prompt)
    else:
        return interactive_loop(agent)


if __name__ == "__main__":
    raise SystemExit(main())
