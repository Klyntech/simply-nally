"""Notion OAuth for MCP — PKCE flow with dynamic client registration.

Notion MCP only supports OAuth 2.0 + PKCE (no device flow, no PAT for remote).
This module handles the full flow:

    discover_oauth_metadata() -> register_client() -> build_auth_url()
    -> browser -> callback -> exchange_code() -> cache token

Discovery follows RFC 9470 (Protected Resource Metadata) -> RFC 8414
(Authorization Server Metadata). Client registration uses RFC 7591.

Token cache: ``~/.config/simply-nally/notion_oauth_token.json``

Access tokens last ~8 hours. Refresh tokens rotate on every use and expire
after 180 days max or 30 days idle.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
NOTION_MCP_URL = os.getenv("NOTION_MCP_URL", "https://mcp.notion.com/mcp").strip()
CACHE_FILE = os.path.expanduser("~/.config/simply-nally/notion_oauth_token.json")
CALLBACK_PORT = int(os.getenv("NOTION_CALLBACK_PORT", "8080"))

# Dynamic -- discovered at runtime via RFC 9470 -> RFC 8414
_AUTH_ENDPOINT: str | None = None
_TOKEN_ENDPOINT: str | None = None
_REGISTRATION_ENDPOINT: str | None = None


# ---------------------------------------------------------------------------
# Cache helpers -- Notion-specific (stores refresh_token too)
# ---------------------------------------------------------------------------
def _cache_path() -> Path:
    return Path(CACHE_FILE).expanduser()


def _read_cache() -> dict[str, Any] | None:
    p = _cache_path()
    try:
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("access_token"):
            return data
        return None
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(data: dict[str, Any]) -> None:
    p = _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        import contextlib

        with contextlib.suppress(Exception):
            os.chmod(tmp, 0o600)
        tmp.replace(p)
        with contextlib.suppress(Exception):
            os.chmod(p, 0o600)
    except OSError as exc:
        logger.warning("Cannot write Notion token cache at %s: %s", p, exc)


def _token_is_valid() -> bool:
    data = _read_cache()
    if not data:
        return False
    expires_at = data.get("expires_at", 0)
    try:
        return time.time() < float(expires_at)
    except (TypeError, ValueError):
        return False


def get_notion_token() -> str | None:
    """Return cached Notion access_token if valid, else None."""
    data = _read_cache()
    if not data:
        return None
    expires_at = data.get("expires_at", 0)
    try:
        if time.time() >= float(expires_at):
            return None
    except (TypeError, ValueError):
        return None
    token = data.get("access_token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def is_notion_authenticated() -> bool:
    """Check if Notion MCP auth is available (env token or cached OAuth)."""
    env_token = os.getenv("NOTION_TOKEN", "").strip()
    if env_token:
        return True
    try:
        return get_notion_token() is not None
    except Exception:
        return False


def clear_notion_token() -> bool:
    """Remove cached Notion token. Returns True if file was removed."""
    p = _cache_path()
    try:
        if p.exists():
            p.unlink()
            return True
        return False
    except OSError:
        return False


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------
def _pkce_challenge() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _state() -> str:
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# OAuth discovery -- RFC 9470 + RFC 8414
# ---------------------------------------------------------------------------
def discover_oauth_metadata(mcp_url: str = NOTION_MCP_URL) -> dict[str, Any]:
    """Discover OAuth endpoints for Notion MCP.

    Step 1: RFC 9470 -- fetch Protected Resource Metadata
    Step 2: RFC 8414 -- fetch Authorization Server Metadata
    """
    global _AUTH_ENDPOINT, _TOKEN_ENDPOINT, _REGISTRATION_ENDPOINT

    url = urllib.parse.urlparse(mcp_url)
    base = f"{url.scheme}://{url.netloc}"

    # Step 1: Protected Resource Metadata
    prm_url = f"{base}/.well-known/oauth-protected-resource"
    resp = requests.get(prm_url, timeout=10)
    resp.raise_for_status()
    prm = resp.json()

    auth_servers = prm.get("authorization_servers", [])
    if not auth_servers:
        raise RuntimeError("No authorization servers found in Notion MCP metadata")

    auth_server_url = auth_servers[0]

    # Step 2: Authorization Server Metadata
    asm_url = f"{auth_server_url}/.well-known/oauth-authorization-server"
    resp2 = requests.get(asm_url, timeout=10)
    resp2.raise_for_status()
    asm = resp2.json()

    _AUTH_ENDPOINT = asm.get("authorization_endpoint")
    _TOKEN_ENDPOINT = asm.get("token_endpoint")
    _REGISTRATION_ENDPOINT = asm.get("registration_endpoint")

    if not _AUTH_ENDPOINT or not _TOKEN_ENDPOINT:
        raise RuntimeError("Missing required OAuth endpoints in Notion metadata")

    return asm


# ---------------------------------------------------------------------------
# Dynamic client registration -- RFC 7591
# ---------------------------------------------------------------------------
def register_client(redirect_uri: str) -> dict[str, str]:
    """Register a dynamic OAuth client with Notion MCP."""
    if not _REGISTRATION_ENDPOINT:
        raise RuntimeError(
            "Notion MCP does not support dynamic client registration. "
            "Set NOTION_CLIENT_ID manually."
        )

    registration = {
        "client_name": "Simply NALLY MCP Client",
        "client_uri": "https://github.com/Klyntech/simply-nally",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }

    resp = requests.post(
        _REGISTRATION_ENDPOINT,
        json=registration,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    creds = resp.json()

    if "client_id" not in creds:
        raise RuntimeError(f"Dynamic client registration failed: {creds}")

    logger.info("Registered Notion OAuth client: %s", creds.get("client_id", "")[:12] + "...")
    return creds


# ---------------------------------------------------------------------------
# Build authorization URL
# ---------------------------------------------------------------------------
def build_auth_url(
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scopes: str | None = None,
) -> str:
    """Build Notion OAuth authorization URL with PKCE params."""
    if not _AUTH_ENDPOINT:
        raise RuntimeError("OAuth metadata not discovered. Call discover_oauth_metadata() first.")

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "consent",
    }
    if scopes:
        params["scope"] = scopes

    return f"{_AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------
def _exchange_code(
    code: str,
    code_verifier: str,
    client_id: str,
    redirect_uri: str,
    client_secret: str | None = None,
) -> dict[str, Any]:
    """Exchange authorization code for tokens. Caches and returns token data."""
    if not _TOKEN_ENDPOINT:
        raise RuntimeError("OAuth metadata not discovered. Call discover_oauth_metadata() first.")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret

    resp = requests.post(
        _TOKEN_ENDPOINT,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    tdata = resp.json()

    if "access_token" not in tdata:
        raise RuntimeError(f"Notion OAuth token exchange failed: {tdata}")

    # Cache the full token data (includes refresh_token, expires_in, etc.)
    expires_at = time.time() + tdata.get("expires_in", 28800)  # default 8h
    cache_data: dict[str, Any] = {
        "access_token": tdata["access_token"],
        "expires_at": expires_at,
    }
    if "refresh_token" in tdata:
        cache_data["refresh_token"] = tdata["refresh_token"]
    if "workspace_id" in tdata:
        cache_data["workspace_id"] = tdata["workspace_id"]
    if "user_id" in tdata:
        cache_data["user_id"] = tdata["user_id"]

    _write_cache(cache_data)
    logger.info("Notion OAuth token cached (expires in %ds)", tdata.get("expires_in", 28800))
    return tdata


# ---------------------------------------------------------------------------
# Callback server (CLI: standalone HTTPServer / Render: Starlette route)
# ---------------------------------------------------------------------------

# Shared registry for Starlette callback — keyed by state -> {code, error}
_callback_registry: dict[str, dict[str, str | None]] = {}


class _CallbackHandler(BaseHTTPRequestHandler):
    """Handles OAuth redirect callback (CLI mode only)."""

    code: str | None = None
    state: str | None = None
    error: str | None = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/notion/callback":
            qs = urllib.parse.parse_qs(parsed.query)
            _CallbackHandler.code = qs.get("code", [None])[0]
            _CallbackHandler.state = qs.get("state", [None])[0]
            _CallbackHandler.error = qs.get("error", [None])[0]
            # Also write to shared registry (for Starlette mode)
            st = _CallbackHandler.state
            if st:
                _callback_registry[st] = {
                    "code": _CallbackHandler.code,
                    "error": _CallbackHandler.error,
                }
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h1>Notion OAuth authorized!</h1>"
                b"<p>You can close this tab.</p></body></html>"
            )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


async def notion_callback_route(request: Any) -> Any:
    """Starlette route handler for /notion/callback.

    Stores the OAuth code/error in _callback_registry keyed by state,
    so notion.py's poll_connection() can pick it up.
    """
    from starlette.responses import HTMLResponse

    qs = urllib.parse.parse_qs(request.url.query)
    code = qs.get("code", [None])[0]
    state = qs.get("state", [None])[0]
    error = qs.get("error", [None])[0]

    if state:
        _callback_registry[state] = {"code": code, "error": error}
        logger.info("Notion OAuth callback received (state=%s)", state[:8])

    return HTMLResponse(
        "<html><body><h1>Notion OAuth authorized!</h1><p>You can close this tab.</p></body></html>"
    )


def _get_redirect_uri() -> str:
    """Build callback URL based on environment."""
    base_url = os.getenv("WEBHOOK_BASE_URL", "").strip()
    if base_url:
        return f"{base_url.rstrip('/')}/notion/callback"
    return f"http://localhost:{CALLBACK_PORT}/notion/callback"


# ---------------------------------------------------------------------------
# Full OAuth flow -- main entry point
# ---------------------------------------------------------------------------
def get_notion_token_via_oauth(
    *,
    timeout: int = 300,
) -> str:
    """Full Notion OAuth PKCE flow (synchronous).

    Args:
        timeout: Max seconds to wait for callback (default 5 min).

    Returns:
        Access token string.

    Raises:
        RuntimeError: On timeout, denial, or exchange failure.
    """
    # Check cache first
    cached = get_notion_token()
    if cached:
        return cached

    # Discover OAuth metadata
    logger.info("Discovering Notion OAuth metadata...")
    discover_oauth_metadata()

    # Determine redirect URI
    redirect_uri = _get_redirect_uri()

    # Register client dynamically (or use env var)
    client_id = os.getenv("NOTION_CLIENT_ID", "").strip()
    client_secret = os.getenv("NOTION_CLIENT_SECRET", "").strip()

    if not client_id:
        if not _REGISTRATION_ENDPOINT:
            raise RuntimeError(
                "NOTION_CLIENT_ID not set and dynamic registration not available. "
                "Set NOTION_CLIENT_ID or configure Notion MCP OAuth."
            )
        logger.info("Registering dynamic Notion OAuth client...")
        creds = register_client(redirect_uri)
        client_id = creds["client_id"]
        client_secret = creds.get("client_secret")

    # Generate PKCE params
    verifier, challenge = _pkce_challenge()
    st = _state()

    # Build auth URL
    auth_url = build_auth_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=challenge,
        state=st,
    )

    logger.info("Notion OAuth: visit %s", auth_url)
    logger.info("Starting callback server on port %d...", CALLBACK_PORT)

    # Reset callback state
    _CallbackHandler.code = None
    _CallbackHandler.state = None
    _CallbackHandler.error = None

    # Start callback server
    server = HTTPServer(("0.0.0.0", CALLBACK_PORT), _CallbackHandler)
    server.timeout = timeout
    start = time.monotonic()

    try:
        while True:
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                raise TimeoutError("Notion OAuth callback timed out")
            server.timeout = min(remaining, 5)
            server.handle_request()
            if _CallbackHandler.code is not None or _CallbackHandler.error is not None:
                break
            if time.monotonic() - start >= timeout:
                raise TimeoutError("Notion OAuth callback timed out")
    except TimeoutError as exc:
        server.server_close()
        raise RuntimeError(
            f"Notion OAuth timed out after {timeout}s waiting for callback."
        ) from exc
    except Exception:
        server.server_close()
        raise
    else:
        server.server_close()

    code = _CallbackHandler.code
    received_state = _CallbackHandler.state
    error = _CallbackHandler.error

    if error:
        raise RuntimeError(f"Notion OAuth denied by user: {error}")
    if not code:
        raise RuntimeError("Notion OAuth: no code received from callback.")
    if received_state != st:
        raise RuntimeError("Notion OAuth: state mismatch (possible CSRF).")

    # Exchange code for tokens
    logger.info("Exchanging Notion OAuth code for tokens...")
    _exchange_code(
        code=code,
        code_verifier=verifier,
        client_id=client_id,
        redirect_uri=redirect_uri,
        client_secret=client_secret or None,
    )

    logger.info("Notion OAuth successful -- token cached.")
    return get_notion_token() or ""
