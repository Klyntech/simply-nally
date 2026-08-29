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

import contextlib
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


def _device_client_config() -> tuple[str, str] | None:
    cid = os.getenv("GOOGLE_DEVICE_CLIENT_ID", "").strip()
    csec = os.getenv("GOOGLE_DEVICE_CLIENT_SECRET", "").strip()
    if not cid or not csec:
        return None
    return cid, csec


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


def validate_device_oauth_config() -> list[str]:
    errs: list[str] = []
    if not os.getenv("GOOGLE_DEVICE_CLIENT_ID", "").strip():
        errs.append("GOOGLE_DEVICE_CLIENT_ID not set (TVs and Limited Input type)")
    if not os.getenv("GOOGLE_DEVICE_CLIENT_SECRET", "").strip():
        errs.append("GOOGLE_DEVICE_CLIENT_SECRET not set")
    try:
        import requests  # noqa: F401
    except ImportError:
        errs.append("requests not installed. Run: pip install requests")
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


# ---------------------------------------------------------------------------
# Google Device Flow (RFC 8628) — for Telegram linking
# ---------------------------------------------------------------------------

GOOGLE_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"


def device_flow_request_code(
    client_id: str | None = None,
    scopes: str | None = None,
) -> dict[str, Any]:
    """Request a device code. Returns {device_code, user_code, verification_url, interval, expires_in}."""
    cid = (client_id or os.getenv("GOOGLE_DEVICE_CLIENT_ID", "").strip()).strip()
    if not cid:
        raise RuntimeError("GOOGLE_DEVICE_CLIENT_ID not set")
    scope = scopes or " ".join(SCOPES)
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("requests not installed. Run: pip install requests") from exc
    resp = requests.post(
        GOOGLE_DEVICE_CODE_URL,
        data={"client_id": cid, "scope": scope},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Device code request failed ({resp.status_code}): {resp.text[:500]}")
    data = resp.json()
    # Google returns verification_url (not verification_uri)
    if "user_code" not in data or "device_code" not in data:
        raise RuntimeError(f"Unexpected device code response: {data}")
    return data


def device_flow_poll_token(
    device_code: str,
    client_id: str | None = None,
    client_secret: str | None = None,
    interval: int = 5,
    expires_in: int = 1800,
    on_status: Any | None = None,
) -> dict[str, Any]:
    """Poll token endpoint until authorized. Handles RFC 8628 errors. Returns token dict on success."""
    cfg = _device_client_config()
    if cfg is not None:
        cid, csec = cfg
        cid = client_id or cid
        csec = client_secret or csec
    else:
        cid = (client_id or "").strip()
        csec = (client_secret or "").strip()
    if not cid or not csec:
        raise RuntimeError("GOOGLE_DEVICE_CLIENT_ID / SECRET not set")
    grant_type = "urn:ietf:params:oauth:grant-type:device_code"
    import time

    import requests

    current_interval = max(1, int(interval))
    deadline = time.time() + max(30, int(expires_in))
    while True:
        if time.time() >= deadline:
            raise RuntimeError("Device code expired. Please send /link again.")
        time.sleep(current_interval)
        resp = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": cid,
                "client_secret": csec,
                "device_code": device_code,
                "grant_type": grant_type,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"error": "invalid_response", "error_description": resp.text[:500]}
        if resp.status_code == 200 and "access_token" in data:
            return data
        error = str(data.get("error", "")).strip().lower()
        if error == "authorization_pending":
            if on_status:
                with contextlib.suppress(Exception):
                    on_status("authorization_pending")
            continue
        if error == "slow_down":
            current_interval += 5
            if on_status:
                with contextlib.suppress(Exception):
                    on_status("slow_down")
            continue
        if error in ("access_denied", "expired_token"):
            raise RuntimeError(f"Authorization {error.replace('_', ' ')}. Please try again.")
        # Unexpected
        desc = data.get("error_description", "")
        raise RuntimeError(f"Device flow error ({error}): {desc or data}")


def decode_google_id_token(id_token_str: str, client_id: str | None = None) -> dict[str, Any]:
    """Verify RS256 signature via Google JWKS and return decoded claims."""
    cid = (
        client_id
        or os.getenv("GOOGLE_DEVICE_CLIENT_ID", "").strip()
        or os.getenv("GOOGLE_CLIENT_ID", "").strip()
    ).strip()
    if not cid:
        raise RuntimeError("GOOGLE_CLIENT_ID not set for ID token verification")
    if not id_token_str or not id_token_str.strip():
        raise RuntimeError("Empty ID token")
    # Try PyJWT path first
    try:
        import jwt
        from jwt import PyJWKClient

        jwks_client = PyJWKClient(GOOGLE_JWKS_URL)
        signing_key = jwks_client.get_signing_key_from_jwt(id_token_str)
        payload = jwt.decode(
            id_token_str,
            signing_key.key,
            algorithms=[signing_key.algorithm_name],
            audience=cid,
            issuer="https://accounts.google.com",
            options={"require": ["exp", "iss", "sub", "aud", "iat"]},
        )
        return dict(payload)
    except ImportError:
        pass
    # Fallback: google-auth library
    try:
        from google.auth.transport import requests as greq
        from google.oauth2 import id_token as git

        req = greq.Request()
        return dict(git.verify_oauth2_token(id_token_str, req, audience=cid))
    except Exception as exc:
        raise RuntimeError(f"ID token verification failed: {exc}") from exc


def link_telegram_to_google(
    telegram_id: str | int,
    *,
    first_name: str | None = None,
    username: str | None = None,
    id_token_str: str | None = None,
    userinfo: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Link a Telegram user to a Google account. `userinfo` must contain sub/email
    (from decoded ID token) or provide id_token_str to decode.

    Creates the Google user if needed, then sets telegram_id. Shared session.
    Returns {user, session, created_google_user: bool}.
    """
    if userinfo is None:
        if not id_token_str:
            raise ValueError("Need id_token_str or userinfo")
        userinfo = decode_google_id_token(id_token_str)
    google_id = str(userinfo.get("sub") or userinfo.get("id") or "").strip()
    email = str(userinfo.get("email") or "").strip()
    if not google_id or not email:
        raise RuntimeError(f"ID token missing sub/email: {userinfo}")
    name = str(userinfo.get("name") or first_name or username or "").strip() or None
    picture = str(userinfo.get("picture") or "").strip() or None
    tid = str(telegram_id).strip()

    from . import db

    if not db.is_configured():
        raise RuntimeError("DATABASE_URL not set")
    conn = db.connect()
    try:
        db.init_schema(conn)
        # Check if telegram already linked to someone
        existing_tg = db.get_user_by_telegram_id(conn, tid)
        if existing_tg is not None and str(existing_tg.get("google_id") or "") != google_id:
            raise RuntimeError(
                f"This Telegram account is already linked to {existing_tg.get('email')}. Send /unlink first."
            )
        existing_google = db.get_user_by_google_id(conn, google_id)
        if existing_google is not None:
            # Link telegram to existing Google user
            user = db.link_telegram_to_user(conn, existing_google["id"], tid)
            session = db.get_or_create_session(conn, user["id"])
            return {"user": user, "session": session, "created_google_user": False}
        # No Google user yet — upsert then link
        user = db.upsert_user(conn, google_id=google_id, email=email, name=name, picture=picture)
        # Need to set telegram_id (upsert doesn't set it)
        user = db.link_telegram_to_user(conn, user["id"], tid)
        session = db.get_or_create_session(conn, user["id"])
        return {"user": user, "session": session, "created_google_user": True}
    finally:
        conn.close()


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
