"""Notion OAuth provider — PKCE Authorization Code flow with discovery.

Notion MCP only supports OAuth 2.0 + PKCE (no device flow, no PAT for remote).
This provider handles discovery, dynamic registration, auth URL, and token exchange.

On SaaS, the OAuth callback is received by the central HTTPS endpoint:
    https://nally.domain.com/oauth/callback/notion
which maps state → OAuthFlowStore → TokenStore.

Discovery follows RFC 9470 (Protected Resource Metadata) → RFC 8414
(Authorization Server Metadata). Client registration uses RFC 7591.

Token lifetime: ~8 hours. Refresh tokens rotate on every use, expire after
180 days max or 30 days idle. Store refresh_token and refresh via token endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

from nally.oauth.models import OAuthResult, OAuthSession, OAuthToken

logger = logging.getLogger(__name__)

NOTION_MCP_URL = os.getenv("NOTION_MCP_URL", "https://mcp.notion.com/mcp").strip()

# Cached discovery results (not per-user, safe to share)
_AUTH_ENDPOINT: str | None = None
_TOKEN_ENDPOINT: str | None = None
_REGISTRATION_ENDPOINT: str | None = None


class NotionOAuthError(Exception):
    """Raised when Notion OAuth operations fail."""


def _get_redirect_uri() -> str:
    """Build callback URL based on environment.

    For SaaS: uses WEBHOOK_BASE_URL or OAUTH_BASE_URL
    For local dev: http://localhost:PORT/oauth/callback/notion
    """
    base_url = os.getenv("OAUTH_BASE_URL", "").strip() or os.getenv("WEBHOOK_BASE_URL", "").strip()
    if base_url:
        return f"{base_url.rstrip('/')}/oauth/callback/notion"
    port = os.getenv("NOTION_CALLBACK_PORT", "8080")
    # For backwards compatibility, support old path /notion/callback
    # but prefer new /oauth/callback/notion
    return f"http://localhost:{port}/oauth/callback/notion"


def discover_oauth_metadata(mcp_url: str = NOTION_MCP_URL) -> dict[str, Any]:
    """Discover OAuth endpoints for Notion MCP.

    Step 1: RFC 9470 — fetch Protected Resource Metadata
    Step 2: RFC 8414 — fetch Authorization Server Metadata
    """
    global _AUTH_ENDPOINT, _TOKEN_ENDPOINT, _REGISTRATION_ENDPOINT

    # Return cached if already discovered
    if _AUTH_ENDPOINT and _TOKEN_ENDPOINT:
        return {
            "authorization_endpoint": _AUTH_ENDPOINT,
            "token_endpoint": _TOKEN_ENDPOINT,
            "registration_endpoint": _REGISTRATION_ENDPOINT,
        }

    url = urllib.parse.urlparse(mcp_url)
    base = f"{url.scheme}://{url.netloc}"

    # Step 1: Protected Resource Metadata
    prm_url = f"{base}/.well-known/oauth-protected-resource"
    resp = requests.get(prm_url, timeout=10)
    resp.raise_for_status()
    prm = resp.json()

    auth_servers = prm.get("authorization_servers", [])
    if not auth_servers:
        raise NotionOAuthError("No authorization servers found in Notion MCP metadata")

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
        raise NotionOAuthError("Missing required OAuth endpoints in Notion metadata")

    return asm


def register_client(redirect_uri: str) -> dict[str, str]:
    """Register a dynamic OAuth client with Notion MCP."""
    if not _REGISTRATION_ENDPOINT:
        raise NotionOAuthError(
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
        raise NotionOAuthError(f"Dynamic client registration failed: {creds}")

    logger.info("Registered Notion OAuth client: %s", creds.get("client_id", "")[:12] + "...")
    return creds


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
        raise NotionOAuthError(
            "OAuth metadata not discovered. Call discover_oauth_metadata() first."
        )

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


class NotionProvider:
    """Notion OAuth via PKCE flow with centralized callback.

    Requires PKCE. Supports dynamic client registration if NOTION_CLIENT_ID
    is not set and discovery indicates registration is available.
    """

    @property
    def provider_name(self) -> str:
        return "notion"

    @property
    def requires_pkce(self) -> bool:
        return True

    @property
    def scopes(self) -> list[str]:
        scopes_str = os.getenv("NOTION_OAUTH_SCOPES", "").strip()
        if not scopes_str:
            return []
        if "," in scopes_str:
            return [s.strip() for s in scopes_str.split(",") if s.strip()]
        return [s.strip() for s in scopes_str.split() if s.strip()]

    async def begin(
        self,
        user_id: str,
        redirect_uri: str,
        state: str,
        code_challenge: str | None = None,
    ) -> OAuthSession:
        """Start Notion OAuth flow.

        Returns authorization_url for user to visit.
        """
        if not code_challenge:
            raise NotionOAuthError("Notion OAuth requires PKCE code_challenge")

        # Discover endpoints
        def _discover():
            return discover_oauth_metadata()

        await asyncio.to_thread(_discover)

        # Resolve redirect_uri — use provided or fall back to env-based
        effective_redirect = redirect_uri or _get_redirect_uri()

        # Resolve client_id
        client_id = os.getenv("NOTION_CLIENT_ID", "").strip()

        if not client_id:
            if not _REGISTRATION_ENDPOINT:
                raise NotionOAuthError(
                    "NOTION_CLIENT_ID not set and dynamic registration not available. "
                    "Set NOTION_CLIENT_ID or configure Notion MCP OAuth."
                )
            creds = await asyncio.to_thread(register_client, effective_redirect)
            client_id = creds["client_id"]

        # Build auth URL
        # Note: need to handle that _AUTH_ENDPOINT is global after discovery
        scopes_joined = " ".join(self.scopes) if self.scopes else None
        auth_url = build_auth_url(
            client_id=client_id,
            redirect_uri=effective_redirect,
            code_challenge=code_challenge,
            state=state,
            scopes=scopes_joined,
        )

        logger.info("Notion OAuth: callback will be received at %s", effective_redirect)
        return OAuthSession(
            state=state,
            provider="notion",
            authorization_url=auth_url,
            code_verifier=None,  # Verifier is stored in flow, not here
        )

    async def callback(
        self,
        code: str,
        state: str,
        code_verifier: str | None = None,
        redirect_uri: str | None = None,
    ) -> OAuthResult:
        """Exchange authorization code for tokens."""
        if not code_verifier:
            raise NotionOAuthError("Missing code_verifier for PKCE exchange")

        # Ensure endpoints discovered
        if not _TOKEN_ENDPOINT:
            await asyncio.to_thread(discover_oauth_metadata)

        # Resolve client_id and redirect_uri
        client_id = os.getenv("NOTION_CLIENT_ID", "").strip()
        client_secret = os.getenv("NOTION_CLIENT_SECRET", "").strip()

        # If not set and we had dynamically registered, we don't have it.
        # In that case, try to discover again? For now, require env.
        # Dynamic registration flow's client_id was used in begin() but
        # not persisted per-flow. We store it in flow metadata ideally.
        # For v1, we assume NOTION_CLIENT_ID is set in production.
        # If empty, attempt to use empty (public client)
        effective_redirect = redirect_uri or _get_redirect_uri()

        # Exchange code for tokens
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": effective_redirect,
            "code_verifier": code_verifier,
        }
        if client_secret:
            data["client_secret"] = client_secret

        # If client_id is empty (dynamic registration) we need to have stored it
        # Fallback: if still empty, try without client_secret
        if not client_id:
            # Public client (PKCE without secret) — omit client_secret
            data.pop("client_secret", None)
            # Try to register again to get client_id? No, that's wrong per-flow.
            # For now raise informative error
            raise NotionOAuthError(
                "NOTION_CLIENT_ID not set. For dynamic registration, the client_id "
                "must be persisted per-flow. Set NOTION_CLIENT_ID for production."
            )

        try:
            resp = await asyncio.to_thread(
                requests.post,
                _TOKEN_ENDPOINT,
                data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                timeout=30,
            )
            resp.raise_for_status()
            tdata = resp.json()
        except requests.RequestException as exc:
            raise NotionOAuthError(f"Token exchange request failed: {exc}") from exc

        if "access_token" not in tdata:
            raise NotionOAuthError(f"Notion OAuth token exchange failed: {tdata}")

        # Fetch workspace info for display
        account = await self._fetch_workspace_name(tdata["access_token"])

        expires_in = tdata.get("expires_in", 28800)
        expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))

        token = OAuthToken(
            provider="notion",
            access_token=tdata["access_token"],
            refresh_token=tdata.get("refresh_token"),
            token_type=tdata.get("token_type", "Bearer"),
            expires_at=expires_at,
            scopes=tuple(self.scopes),
            account=account,
        )

        return OAuthResult(token=token, user_id="", provider="notion")

    async def _fetch_workspace_name(self, token: str) -> str:
        """Fetch Notion workspace name for display."""
        try:
            resp = await asyncio.to_thread(
                requests.get,
                "https://api.notion.com/v1/users/me",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Notion-Version": "2022-06-28",
                },
                timeout=10,
            )
            if resp.ok:
                data = resp.json()
                return data.get("name", "Notion workspace")
        except Exception:
            pass
        return "Notion workspace"

    async def refresh(self, token: OAuthToken) -> OAuthToken:
        """Refresh a Notion token."""
        if not token.refresh_token:
            raise NotionOAuthError("No refresh_token available; re-authentication required.")

        if not _TOKEN_ENDPOINT:
            await asyncio.to_thread(discover_oauth_metadata)

        client_id = os.getenv("NOTION_CLIENT_ID", "").strip()
        client_secret = os.getenv("NOTION_CLIENT_SECRET", "").strip()

        data: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
        }
        if client_id:
            data["client_id"] = client_id
        if client_secret:
            data["client_secret"] = client_secret

        try:
            resp = await asyncio.to_thread(
                requests.post,
                _TOKEN_ENDPOINT,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            resp.raise_for_status()
            tdata = resp.json()
        except requests.RequestException as exc:
            raise NotionOAuthError(f"Refresh request failed: {exc}") from exc

        if "access_token" not in tdata:
            raise NotionOAuthError(f"Notion refresh failed: {tdata}")

        expires_in = tdata.get("expires_in", 28800)
        expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))

        return OAuthToken(
            provider="notion",
            access_token=tdata["access_token"],
            refresh_token=tdata.get("refresh_token", token.refresh_token),
            token_type=tdata.get("token_type", token.token_type),
            expires_at=expires_at,
            scopes=token.scopes,
            account=token.account,
        )

    async def revoke(self, token: OAuthToken) -> bool:
        """Revoke a Notion token (if endpoint available)."""
        # Notion doesn't dokument revoke endpoint in this flow; best-effort
        return True

    async def identity(self, token: OAuthToken):
        """Fetch Notion workspace identity."""
        from nally.auth_broker.models import ProviderIdentity

        name = await self._fetch_workspace_name(token.access_token)
        # For Notion, use workspace name as both subject and display where possible
        # Try to fetch more specific bot info via token metadata
        subject = token.account or name or "notion-workspace"
        # Token account may be workspace name from callback
        return ProviderIdentity(subject=subject, display_name=name or token.account, raw={"workspace": name})

    def format_auth_headers(self, token: OAuthToken) -> dict[str, str]:
        return {"Authorization": f"Bearer {token.access_token}"}

    def format_auth_env(self, token: OAuthToken) -> dict[str, str]:
        return {"NOTION_TOKEN": token.access_token}
