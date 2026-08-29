"""Google OAuth — Desktop (browser) flow for the CLI.

One session per user (v1). Credentials are kept locally in
~/.config/simply-nally/auth.json so the CLI knows who is logged in
across restarts. NEON is the source of truth for users/sessions.

Requires env:
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET  (from Google Cloud Console, Desktop app)
  DATABASE_URL  (NEON connection string)

Deps: google-auth, google-auth-oauthlib, requests
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Local auth file (CLI knows who is logged in without hitting DB every time)
# ---------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".config" / "simply-nally"
AUTH_FILE = CONFIG_DIR / "auth.json"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
# Fallback
GOOGLE_USERINFO_URL_V2 = "https://www.googleapis.com/userinfo/v2/me"

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


def _ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _read_auth_file() -> dict[str, Any] | None:
    if not AUTH_FILE.exists():
        return None
    try:
        data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("user_id"):
            return data
        return None
    except (json.JSONDecodeError, OSError):
        return None


def _write_auth_file(data: dict[str, Any]) -> None:
    _ensure_config_dir()
    tmp = AUTH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(AUTH_FILE)


def get_current_auth() -> dict[str, Any] | None:
    """Return the locally stored auth (user_id, email, etc.) or None if not logged in."""
    return _read_auth_file()


def is_logged_in() -> bool:
    return get_current_auth() is not None


def logout_local() -> bool:
    """Remove local auth file. Returns True if a session was cleared."""
    if AUTH_FILE.exists():
        try:
            AUTH_FILE.unlink()
            return True
        except OSError:
            return False
    return False


# ---------------------------------------------------------------------------
# Google OAuth config helpers
# ---------------------------------------------------------------------------


def _google_client_config() -> dict[str, Any] | None:
    cid = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    csec = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    if not cid or not csec:
        return None
    # google-auth-oauthlib expects this shape for from_client_config
    return {
        "installed": {
            "client_id": cid,
            "client_secret": csec,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def validate_oauth_config() -> list[str]:
    errs: list[str] = []
    if not os.getenv("GOOGLE_CLIENT_ID", "").strip():
        errs.append("GOOGLE_CLIENT_ID not set")
    if not os.getenv("GOOGLE_CLIENT_SECRET", "").strip():
        errs.append("GOOGLE_CLIENT_SECRET not set")
    # Check deps
    try:
        import google_auth_oauthlib  # noqa: F401
    except ImportError:
        errs.append(
            "google-auth-oauthlib not installed. Run: pip install google-auth google-auth-oauthlib requests"
        )
    return errs


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------


def fetch_google_userinfo(credentials) -> dict[str, Any]:
    """Use credentials to fetch user profile. Tries v3 then v2."""
    # credentials with google-auth use authorized session helper
    import requests

    # google.oauth2.credentials.Credentials has .token, use Bearer
    token = getattr(credentials, "token", None)
    if not token:
        raise RuntimeError("Google credentials have no token")

    headers = {"Authorization": f"Bearer {token}"}
    for url in (GOOGLE_USERINFO_URL, GOOGLE_USERINFO_URL_V2):
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                # v3 uses "sub", v2 uses "id" — normalize to "sub"
                if "sub" not in data and "id" in data:
                    data["sub"] = data["id"]
                if "sub" in data and "email" in data:
                    return data
        except Exception:
            continue
    raise RuntimeError(f"Failed to fetch Google userinfo (tried {GOOGLE_USERINFO_URL})")


def login_with_browser(*, open_browser: bool = True, timeout_seconds: int = 120) -> dict[str, Any]:
    """
    Run the Desktop OAuth flow (local server on random port).
    Opens the browser, authenticates, fetches Google profile, upserts to NEON,
    ensures a session, and writes AUTH_FILE.

    Returns {"user": {...}, "session": {...}} on success.
    Raises RuntimeError on config or flow errors.
    """
    cfg = _google_client_config()
    if cfg is None:
        raise RuntimeError("Missing GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET. Set them in .env")

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError(
            "google-auth-oauthlib not installed. Run: pip install google-auth google-auth-oauthlib requests"
        ) from exc

    flow = InstalledAppFlow.from_client_config(cfg, scopes=SCOPES)

    # run_local_server picks a free port (0) and opens the browser
    # It blocks until the user completes the OAuth consent screen.
    if open_browser:
        print("Opening browser for Google login…")
    else:
        print("Waiting for Google OAuth…")

    # On Windows, wfile may need timeout; use default
    try:
        creds = flow.run_local_server(
            port=0,
            open_browser=open_browser,
            timeout_seconds=timeout_seconds,
            prompt="consent",
        )
    except Exception as exc:
        raise RuntimeError(f"Google OAuth flow failed: {exc}") from exc

    # Fetch userinfo
    userinfo = fetch_google_userinfo(creds)
    google_id = str(userinfo.get("sub") or userinfo.get("id") or "").strip()
    email = str(userinfo.get("email") or "").strip()
    name = str(userinfo.get("name") or userinfo.get("given_name") or "").strip() or None
    picture = str(userinfo.get("picture") or "").strip() or None

    if not google_id or not email:
        raise RuntimeError(f"Google userinfo missing sub/email: {userinfo}")

    # Persist to NEON
    from . import db

    if not db.is_configured():
        raise RuntimeError(
            "DATABASE_URL not set — cannot persist user. Set NEON connection string in .env"
        )

    conn = db.connect()
    try:
        db.init_schema(conn)
        user = db.upsert_user(conn, google_id=google_id, email=email, name=name, picture=picture)
        session = db.get_or_create_session(conn, user["id"])
    finally:
        conn.close()

    # Write local auth file (so CLI knows who we are without DB round-trip)
    auth_data = {
        "user_id": user["id"],
        "google_id": user["google_id"],
        "email": user["email"],
        "name": user["name"],
        "picture": user["picture"],
        "session_id": session["id"],
        "last_login": str(user["last_login"]),
    }
    _write_auth_file(auth_data)

    return {"user": user, "session": session, "userinfo": userinfo}


def ensure_user_and_session() -> dict[str, Any] | None:
    """
    If locally logged in and DB is configured, return {user, session}.
    If not logged in or DB unavailable, return None.
    Used by the agent to load the persisted session.
    """
    auth = get_current_auth()
    if auth is None:
        return None
    from . import db

    if not db.is_configured():
        return None
    try:
        conn = db.connect()
        try:
            user = db.get_user_by_id(conn, auth["user_id"])
            if user is None:
                return None
            session = db.get_or_create_session(conn, user["id"])
            return {"user": user, "session": session}
        finally:
            conn.close()
    except Exception:
        return None
