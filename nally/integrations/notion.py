"""Notion integration — PKCE OAuth with shared callback registry.

Notion MCP requires authorization-code + PKCE flow.
This provider manages the full lifecycle: discovery, registration,
auth URL, callback, and token exchange.

On Render/webhook mode, the OAuth callback is received by the Starlette
route at /notion/callback and stored in a shared registry.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import requests

from . import token_store
from .base import BaseProvider
from .token_store import TokenStoreError

logger = logging.getLogger(__name__)

# Notion MCP endpoints
NOTION_MCP_URL = os.getenv("NOTION_MCP_URL", "https://mcp.notion.com/mcp").strip()


class NotionProvider(BaseProvider):
    """Notion OAuth via PKCE flow with shared callback registry."""

    PROVIDER_NAME = "notion"

    def is_connected(self, user_id: str) -> bool:
        """Check env token or per-user cached token."""
        env_token = os.getenv("NOTION_TOKEN", "").strip()
        if env_token:
            return True
        return token_store.get_valid_token(user_id, "notion") is not None

    async def connect(self, user_id: str) -> dict[str, Any]:
        """Start Notion PKCE flow. Returns auth_url + flow state."""
        from nally.notion_oauth import (
            _get_redirect_uri,
            _pkce_challenge,
            _state,
            build_auth_url,
            discover_oauth_metadata,
        )

        # Discover endpoints
        discover_oauth_metadata()
        redirect_uri = _get_redirect_uri()

        # Register client or use env var
        client_id = os.getenv("NOTION_CLIENT_ID", "").strip()
        client_secret = os.getenv("NOTION_CLIENT_SECRET", "").strip()

        if not client_id:
            from nally.notion_oauth import _REGISTRATION_ENDPOINT, register_client

            if not _REGISTRATION_ENDPOINT:
                raise RuntimeError(
                    "NOTION_CLIENT_ID not set and dynamic registration not available. "
                    "Set NOTION_CLIENT_ID or configure Notion MCP OAuth."
                )
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

        logger.info("Notion OAuth: callback will be received at %s", redirect_uri)
        return {
            "auth_url": auth_url,
            "verifier": verifier,
            "state": st,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        }

    async def poll_connection(self, user_id: str, flow_data: dict[str, Any]) -> bool:
        """Wait for Notion callback to receive auth code via shared registry."""
        from nally.notion_oauth import _callback_registry, _exchange_code

        state = flow_data.get("state")
        timeout = 300  # 5 min

        start = time.time()
        while time.time() - start < timeout:
            entry = _callback_registry.get(state) if state else None
            if entry is not None:
                break
            await asyncio.sleep(2)

        entry = _callback_registry.pop(state, None) if state else None
        if entry is None:
            raise RuntimeError("Notion OAuth timed out -- no code received.")

        code = entry.get("code")
        error = entry.get("error")

        if error:
            raise RuntimeError(f"Notion OAuth denied: {error}")
        if not code:
            raise RuntimeError("Notion OAuth: no code received from callback.")

        # Exchange code for tokens
        tdata = _exchange_code(
            code=code,
            code_verifier=flow_data["verifier"],
            client_id=flow_data["client_id"],
            redirect_uri=flow_data["redirect_uri"],
            client_secret=flow_data.get("client_secret"),
        )

        # Store token per-user
        expires_at = time.time() + tdata.get("expires_in", 28800)
        token_data: dict[str, Any] = {
            "access_token": tdata["access_token"],
            "expires_at": expires_at,
        }
        if "refresh_token" in tdata:
            token_data["refresh_token"] = tdata["refresh_token"]
        if "workspace_id" in tdata:
            token_data["workspace_id"] = tdata["workspace_id"]

        # Fetch workspace info for display
        try:
            token_data["account"] = await self._fetch_workspace_name(tdata["access_token"])
        except Exception:
            token_data["account"] = "Notion workspace"

        try:
            token_store.write_token(user_id, "notion", token_data)
        except TokenStoreError as exc:
            raise RuntimeError(
                f"Notion OAuth succeeded but NALLY could not save the credential: {exc}"
            ) from exc

        logger.info("Notion token cached for user %s", user_id)
        return True

    async def _fetch_workspace_name(self, token: str) -> str:
        """Fetch Notion workspace name for display."""
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
            return data.get("name", "Notion user")
        return "Notion workspace"

    def disconnect(self, user_id: str) -> bool:
        """Remove per-user cached token."""
        return token_store.clear_token(user_id, "notion")

    def get_account_info(self, user_id: str) -> str | None:
        """Return workspace name or None."""
        return token_store.get_account_info(user_id, "notion")

    def get_auth_headers(self, user_id: str) -> dict[str, str] | None:
        """Return Authorization header for MCP HTTP transport."""
        token = token_store.get_valid_token(user_id, "notion")
        if token:
            return {"Authorization": f"Bearer {token}"}
        env_token = os.getenv("NOTION_TOKEN", "").strip()
        if env_token:
            return {"Authorization": f"Bearer {env_token}"}
        return None

    def get_auth_env(self, user_id: str) -> dict[str, str] | None:
        """Return env vars for MCP stdio transport."""
        token = token_store.get_valid_token(user_id, "notion")
        if token:
            return {"NOTION_TOKEN": token}
        env_token = os.getenv("NOTION_TOKEN", "").strip()
        if env_token:
            return {"NOTION_TOKEN": env_token}
        return None
