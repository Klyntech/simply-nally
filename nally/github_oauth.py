"""GitHub OAuth for MCP — device flow (primary) + PKCE (experimental).

Delegates token-cache handling to ``nally.mcp.auth`` (single source of
truth for file I/O, permissions, and logging). This module remains
GitHub-specific by design; generic MCP auth lives in ``nally.mcp.auth``.

Device flow is the current Telegram UX:
    github_request_device_code() -> user enters code -> github_poll_token()

PKCE flow (authorization code) is experimental for local browser auth:
    build_auth_url() -> browser -> callback -> _exchange_code()

Stored token is plaintext JSON under ``~/.config/simply-nally/`` with
``0o600`` (see ``nally.mcp.auth``). Documented limitation.

Scope default is broad (``repo,read:user,workflow,…``) for the MCP
experiment. Override via ``GITHUB_OAUTH_SCOPES`` env or ``scopes`` param
for least-privilege deployments.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

# Cache delegation — single place for file handling, logging, perms
from nally.mcp.auth import (
    DEFAULT_CACHE_FILE as _DEFAULT_CACHE_FILE,
)
from nally.mcp.auth import (
    clear_token_cache as _clear_cache,
)
from nally.mcp.auth import (
    get_cached_token as _auth_get_cached_token,
)
from nally.mcp.auth import (
    read_token_cache as _auth_read_cache,
)
from nally.mcp.auth import (
    token_is_valid as _auth_token_is_valid,
)
from nally.mcp.auth import (
    write_token_cache as _auth_write_cache,
)

# ---------------------------------------------------------------------------
# Constants (keep for backwards compat)
# ---------------------------------------------------------------------------
GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"

TOKEN_CACHE_FILE = _DEFAULT_CACHE_FILE
CALLBACK_PORT = 8080
CALLBACK_URL = f"http://localhost:{CALLBACK_PORT}/callback"


# ---------------------------------------------------------------------------
# Cache helpers — thin wrappers that remain patchable via os.* for tests
# ---------------------------------------------------------------------------
def _read_token_cache() -> dict | None:
    # Delegate to canonical impl (handles logging, corrupt JSON, etc.)
    return _auth_read_cache()


def _write_token_cache(token: str, expires_at: float) -> None:
    _auth_write_cache(token, expires_at)


def _token_is_valid() -> bool:
    return _auth_token_is_valid()


def get_cached_token() -> str | None:
    """Return cached token if valid, else None."""
    return _auth_get_cached_token()


def is_github_authenticated() -> bool:
    """Check if GitHub MCP auth is available (PAT or cached token)."""
    pat = (
        os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "").strip()
        or os.getenv("GITHUB_TOKEN", "").strip()
    )
    if pat:
        return True
    try:
        return get_cached_token() is not None
    except Exception:
        return False


def clear_github_token() -> bool:
    """Remove cached token. Returns True if file was removed.

    Uses ``os.path.exists`` / ``os.remove`` so tests can patch them.
    Falls back to ``nally.mcp.auth.clear_token_cache`` if needed.
    """
    try:
        if os.path.exists(TOKEN_CACHE_FILE):
            os.remove(TOKEN_CACHE_FILE)
            return True
        return False
    except Exception:
        # Fallback: try canonical clear (pathlib-based)
        try:
            return _clear_cache()
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Device flow — helpers
# ---------------------------------------------------------------------------
def _poll_token_response(
    device_code: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """Single poll request. Returns parsed JSON."""
    resp = requests.post(
        GITHUB_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _handle_poll_result(tdata: dict, interval: int) -> tuple[str | None, int | None]:
    """Inspect poll JSON.

    Returns (token, sleep_seconds) or (None, sleep) or raises.
    """
    if "access_token" in tdata:
        return tdata["access_token"], None

    error = tdata.get("error", "")
    if error == "authorization_pending":
        return None, max(interval, 1)
    if error == "slow_down":
        return None, interval + 5
    if error == "expired_token":
        raise RuntimeError("GitHub device code expired. Please retry.")
    if error == "access_denied":
        raise RuntimeError("GitHub OAuth access denied by user.")
    if error:
        raise RuntimeError(f"GitHub OAuth unexpected response: {tdata}")
    raise RuntimeError(f"GitHub OAuth unexpected response: {tdata}")


def github_request_device_code() -> dict:
    """Request a GitHub device code. Returns dict with device_code, user_code, etc."""
    cid = os.getenv("GITHUB_CLIENT_ID", "").strip()
    csec = os.getenv("GITHUB_CLIENT_SECRET", "").strip()
    if not cid or not csec:
        raise RuntimeError(
            "GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET not set. "
            "Register an OAuth App at https://github.com/settings/developers"
        )

    device_scopes = os.getenv(
        "GITHUB_OAUTH_SCOPES", "repo,read:user,workflow,pull_requests,issues"
    ).strip()

    resp = requests.post(
        GITHUB_DEVICE_CODE_URL,
        data={"client_id": cid, "scope": device_scopes},
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def github_poll_token(
    device_code: str,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    expires_in: int = 900,
    interval: int = 5,
) -> str:
    """Poll for GitHub access token. Blocking — run in thread if needed.

    Uses shared poll handling so logic isn't duplicated with
    ``get_github_token``.
    """
    cid = (client_id or os.getenv("GITHUB_CLIENT_ID", "")).strip()
    csec = (client_secret or os.getenv("GITHUB_CLIENT_SECRET", "")).strip()
    deadline = time.time() + expires_in
    while time.time() < deadline:
        tdata = _poll_token_response(device_code, cid, csec)

        token, sleep_for = _handle_poll_result(tdata, interval)
        if token:
            # Success — cache and return
            expires_at = time.time() + tdata.get("expires_in", 3600)
            _write_token_cache(token, expires_at)
            return token
        if sleep_for is not None:
            time.sleep(sleep_for)
            continue

    raise RuntimeError("GitHub OAuth device flow timed out.")


async def get_github_token(
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    scopes: str | None = None,
    timeout: int = 120,
) -> str:
    """Get a GitHub access token via device flow.

    Async entry point for Telegram UX. Blocks until user authorizes
    (or timeout). Internally uses ``asyncio.sleep`` so it doesn't
    block the event loop, but ``requests`` calls are still synchronous
    and should ideally be run in a thread for production. For now we
    keep ``requests`` for simplicity and use ``asyncio.sleep`` for
    polling delays.

    Timeout controls the overall wait (not just one poll). Default 120s.
    """
    import asyncio

    cid = (client_id or os.getenv("GITHUB_CLIENT_ID", "")).strip()
    csec = (client_secret or os.getenv("GITHUB_CLIENT_SECRET", "")).strip()
    if not cid or not csec:
        raise RuntimeError(
            "GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET not set. "
            "Register an OAuth App at https://github.com/settings/developers"
        )

    if _token_is_valid():
        tok = get_cached_token()
        if tok:
            return tok

    device_scopes = (
        scopes
        or os.getenv("GITHUB_OAUTH_SCOPES", "repo,read:user,workflow,pull_requests,issues").strip()
    )

    # Request device code (blocking, but short — run directly)
    resp = requests.post(
        GITHUB_DEVICE_CODE_URL,
        data={"client_id": cid, "scope": device_scopes},
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    device_code = data["device_code"]
    user_code = data["user_code"]
    verification_uri = data.get("verification_uri", "https://github.com/login/device")
    expires_in = data.get("expires_in", 900)
    interval = data.get("interval", 5)

    # Bound by both device expiry and caller timeout
    effective_timeout = min(expires_in, timeout) if timeout else expires_in
    print(f"\nGitHub OAuth: visit {verification_uri} and enter code: {user_code}")
    print(f"Waiting for authorization (expires in {effective_timeout}s)...\n")

    deadline = time.time() + effective_timeout
    while time.time() < deadline:
        tdata = _poll_token_response(device_code, cid, csec)

        token, sleep_for = _handle_poll_result(tdata, interval)
        if token:
            expires_at = time.time() + tdata.get("expires_in", 3600)
            _write_token_cache(token, expires_at)
            print("GitHub OAuth: token obtained.\n")
            return token
        if sleep_for is not None:
            # Use async sleep so event loop isn't blocked
            # Cap sleep to remaining time
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(sleep_for, remaining))
            continue

    raise RuntimeError("GitHub OAuth device flow timed out.")


# ---------------------------------------------------------------------------
# PKCE flow — experimental browser-based auth (kept for reference)
# ---------------------------------------------------------------------------
def _pkce_challenge() -> tuple[str, str]:
    import base64

    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _state() -> str:
    return secrets.token_urlsafe(32)


def build_auth_url(
    *,
    client_id: str | None = None,
    scopes: str | None = None,
    state: str | None = None,
) -> str:
    """Build GitHub OAuth authorization URL for PKCE flow."""
    cid = (client_id or os.getenv("GITHUB_CLIENT_ID", "")).strip()
    # Keep broad default but allow override for least-privilege
    default_scopes = "repo,read:user,workflow"
    device_scopes = (scopes or os.getenv("GITHUB_OAUTH_SCOPES", default_scopes)).strip()
    st = state or _state()
    _verifier, challenge = _pkce_challenge()

    params = {
        "client_id": cid,
        "redirect_uri": CALLBACK_URL,
        "scope": device_scopes,
        "state": st,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "response_type": "code",
        "prompt": "consent",
    }
    return f"{GITHUB_AUTH_URL}?{urllib.parse.urlencode(params)}"


class _CallbackHandler(BaseHTTPRequestHandler):
    code: str | None = None
    state: str | None = None
    error: str | None = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/callback":
            qs = urllib.parse.parse_qs(parsed.query)
            _CallbackHandler.code = qs.get("code", [None])[0]
            _CallbackHandler.state = qs.get("state", [None])[0]
            _CallbackHandler.error = qs.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h1>GitHub OAuth authorized!</h1>"
                b"<p>You can close this tab.</p></body></html>"
            )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args) -> None:
        pass


def _exchange_code(code: str, verifier: str, state: str) -> str:
    cid = os.getenv("GITHUB_CLIENT_ID", "").strip()
    csec = os.getenv("GITHUB_CLIENT_SECRET", "").strip()

    data = {
        "client_id": cid,
        "client_secret": csec,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": CALLBACK_URL,
        "code_verifier": verifier,
        "state": state,
    }
    resp = requests.post(
        GITHUB_TOKEN_URL,
        data=data,
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    tdata = resp.json()

    if "access_token" not in tdata:
        raise RuntimeError(f"GitHub OAuth token exchange failed: {tdata}")

    token = tdata["access_token"]
    expires_at = time.time() + tdata.get("expires_in", 3600)
    _write_token_cache(token, expires_at)
    return token


def get_github_token_via_pkce(
    *,
    client_id: str | None = None,
    scopes: str | None = None,
    timeout: int = 120,
) -> str:
    """PKCE authorization-code flow (synchronous, browser-based).

    Kept for reference / local dev. Do not call from async loop without
    threading. For Telegram, use device flow (``get_github_token``).
    """
    if _token_is_valid():
        tok = get_cached_token()
        if tok:
            return tok

    cid = (client_id or os.getenv("GITHUB_CLIENT_ID", "")).strip()
    if not cid:
        raise RuntimeError(
            "GITHUB_CLIENT_ID not set. "
            "Register an OAuth App at https://github.com/settings/developers"
        )

    verifier, challenge = _pkce_challenge()
    st = _state()

    device_scopes = (
        scopes or os.getenv("GITHUB_OAUTH_SCOPES", "repo,read:user,workflow")
    ).strip()
    params = {
        "client_id": cid,
        "redirect_uri": CALLBACK_URL,
        "scope": device_scopes,
        "state": st,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "response_type": "code",
        "prompt": "consent",
    }
    auth_url = f"{GITHUB_AUTH_URL}?{urllib.parse.urlencode(params)}"

    import webbrowser

    _CallbackHandler.code = None
    _CallbackHandler.state = None
    _CallbackHandler.error = None

    webbrowser.open(auth_url)
    print("\nGitHub OAuth (PKCE): opening browser…\n")
    print(f"If browser did not open, visit:\n{auth_url}\n")

    server = HTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
    server.timeout = timeout
    start = time.monotonic()
    try:
        while True:
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                raise TimeoutError("GitHub OAuth callback timed out")
            server.timeout = min(remaining, 5)
            server.handle_request()
            if _CallbackHandler.code is not None or _CallbackHandler.error is not None:
                break
            if time.monotonic() - start >= timeout:
                raise TimeoutError("GitHub OAuth callback timed out")
    except TimeoutError as exc:
        server.server_close()
        raise RuntimeError(f"GitHub OAuth timed out after {timeout}s waiting for callback.") from exc
    except Exception:
        server.server_close()
        raise
    else:
        server.server_close()

    code = _CallbackHandler.code
    received_state = _CallbackHandler.state
    error = _CallbackHandler.error

    if error:
        raise RuntimeError(f"GitHub OAuth denied by user: {error}")
    if not code:
        raise RuntimeError("GitHub OAuth: no code received from callback.")
    if received_state != st:
        raise RuntimeError("GitHub OAuth: state mismatch possible CSRF attack.")

    return _exchange_code(code, verifier, st)
