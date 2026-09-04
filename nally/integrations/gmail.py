"""Gmail integration — Google device flow OAuth.

Uses Google's OAuth device flow (google.com/device).
Scopes: gmail.readonly + gmail.compose for v1.
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

# Google device flow endpoints
_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Gmail scopes
_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.compose"
)


class GmailProvider(BaseProvider):
    """Gmail OAuth via Google device flow."""

    PROVIDER_NAME = "gmail"

    def is_connected(self, user_id: str) -> bool:
        """Check env token or per-user cached token."""
        env_token = (
            os.getenv("GMAIL_TOKEN", "").strip()
            or os.getenv("GMAIL_OAUTH_TOKEN", "").strip()
            or os.getenv("GOOGLE_GMAIL_TOKEN", "").strip()
        )
        if env_token:
            return True
        return token_store.get_valid_token(user_id, "gmail") is not None

    async def connect(self, user_id: str) -> dict[str, Any]:
        """Start Google device flow."""
        cid = os.getenv("GMAIL_CLIENT_ID", "").strip()
        csec = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
        if not cid or not csec:
            raise RuntimeError(
                "GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET not configured. "
                "Create credentials at https://console.cloud.google.com/apis/credentials"
            )

        scopes = os.getenv("GMAIL_OAUTH_SCOPES", _SCOPES).strip()

        resp = await asyncio.to_thread(
            requests.post,
            _DEVICE_CODE_URL,
            data={
                "client_id": cid,
                "scope": scopes,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        logger.info("Gmail device flow started for user %s", user_id)
        return {
            "device_code": data["device_code"],
            "user_code": data["user_code"],
            "verification_uri": data.get("verification_uri", "https://google.com/device"),
            "expires_in": data.get("expires_in", 600),
            "interval": data.get("interval", 5),
        }

    async def poll_connection(self, user_id: str, flow_data: dict[str, Any]) -> bool:
        """Poll Google token endpoint until authorized or timeout."""
        cid = os.getenv("GMAIL_CLIENT_ID", "").strip()
        csec = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
        device_code = flow_data["device_code"]
        interval = flow_data.get("interval", 5)
        expires_in = flow_data.get("expires_in", 600)

        deadline = time.time() + expires_in
        while time.time() < deadline:
            resp = await asyncio.to_thread(
                requests.post,
                _TOKEN_URL,
                data={
                    "client_id": cid,
                    "client_secret": csec,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            tdata = resp.json()

            if "access_token" in tdata:
                expires_at = time.time() + tdata.get("expires_in", 3600)
                token_data: dict[str, Any] = {
                    "access_token": tdata["access_token"],
                    "expires_at": expires_at,
                }
                if "refresh_token" in tdata:
                    token_data["refresh_token"] = tdata["refresh_token"]

                # Fetch email for display
                try:
                    token_resp = await asyncio.to_thread(
                        requests.get,
                        "https://www.googleapis.com/oauth2/v3/userinfo",
                        headers={"Authorization": f"Bearer {tdata['access_token']}"},
                        timeout=10,
                    )
                    if token_resp.ok:
                        profile = token_resp.json()
                        token_data["account"] = profile.get("email", "Gmail user")
                except Exception:
                    token_data["account"] = "Gmail user"

                try:
                    token_store.write_token(user_id, "gmail", token_data)
                except TokenStoreError as exc:
                    raise RuntimeError(
                        f"Gmail OAuth succeeded but NALLY could not save the credential: {exc}"
                    ) from exc

                logger.info("Gmail token cached for user %s", user_id)
                return True

            error = tdata.get("error", "")
            if error == "authorization_pending":
                await asyncio.sleep(max(interval, 1))
            elif error == "slow_down":
                await asyncio.sleep(interval + 5)
            elif error in ("expired_token", "access_denied"):
                raise RuntimeError(f"Gmail OAuth failed: {error}")
            elif error:
                raise RuntimeError(f"Gmail OAuth unexpected response: {tdata}")
            else:
                raise RuntimeError(f"Gmail OAuth unexpected response: {tdata}")

        raise RuntimeError("Gmail OAuth device flow timed out.")

    def disconnect(self, user_id: str) -> bool:
        """Remove per-user cached token."""
        return token_store.clear_token(user_id, "gmail")

    def get_account_info(self, user_id: str) -> str | None:
        """Return Gmail address or None."""
        return token_store.get_account_info(user_id, "gmail")

    def get_auth_headers(self, user_id: str) -> dict[str, str] | None:
        """Return Authorization header for MCP HTTP transport."""
        token = token_store.get_valid_token(user_id, "gmail")
        if token:
            return {"Authorization": f"Bearer {token}"}
        env_token = (
            os.getenv("GMAIL_TOKEN", "").strip()
            or os.getenv("GMAIL_OAUTH_TOKEN", "").strip()
            or os.getenv("GOOGLE_GMAIL_TOKEN", "").strip()
        )
        if env_token:
            return {"Authorization": f"Bearer {env_token}"}
        return None

    def get_auth_env(self, user_id: str) -> dict[str, str] | None:
        """Return env vars for MCP stdio transport."""
        token = token_store.get_valid_token(user_id, "gmail")
        if token:
            return {"GMAIL_TOKEN": token}
        env_token = (
            os.getenv("GMAIL_TOKEN", "").strip()
            or os.getenv("GMAIL_OAUTH_TOKEN", "").strip()
            or os.getenv("GOOGLE_GMAIL_TOKEN", "").strip()
        )
        if env_token:
            return {"GMAIL_TOKEN": env_token}
        return None
