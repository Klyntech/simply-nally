"""GitHub integration — device flow OAuth.

Default scopes grant full repository access (read + write for private repos).
The "read-only" policy is enforced by convention, not by OAuth scope.
Override via GITHUB_OAUTH_SCOPES env for least-privilege deployments.
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

# Device flow endpoints
_DEVICE_CODE_URL = "https://github.com/login/device/code"
_TOKEN_URL = "https://github.com/login/oauth/access_token"

# Default scopes for v1.
# read:user — read user profile (read-only).
# repo — full repository access (read + write for private repos).
# The "read-only" policy is enforced by convention, not by OAuth scope.
# Override via GITHUB_OAUTH_SCOPES env for least-privilege deployments.
_DEFAULT_SCOPES = "read:user,repo"


class GitHubProvider(BaseProvider):
    """GitHub OAuth via device flow. Read-only by convention."""

    PROVIDER_NAME = "github"

    def is_connected(self, user_id: str) -> bool:
        """Check global PAT or per-user cached token."""
        pat = (
            os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "").strip()
            or os.getenv("GITHUB_TOKEN", "").strip()
        )
        if pat:
            return True
        return token_store.get_valid_token(user_id, "github") is not None

    async def connect(self, user_id: str) -> dict[str, Any]:
        """Start device flow. Returns user_code, verification_uri, etc."""
        cid = os.getenv("GITHUB_CLIENT_ID", "").strip()
        csec = os.getenv("GITHUB_CLIENT_SECRET", "").strip()
        if not cid or not csec:
            raise RuntimeError(
                "GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET not configured. "
                "Register an OAuth App at https://github.com/settings/developers"
            )

        scopes = os.getenv("GITHUB_OAUTH_SCOPES", _DEFAULT_SCOPES).strip()

        resp = await asyncio.to_thread(
            requests.post,
            _DEVICE_CODE_URL,
            data={"client_id": cid, "scope": scopes},
            headers={"Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        logger.info("GitHub device flow started for user %s", user_id)
        return {
            "device_code": data["device_code"],
            "user_code": data["user_code"],
            "verification_uri": data.get("verification_uri", "https://github.com/login/device"),
            "expires_in": data.get("expires_in", 900),
            "interval": data.get("interval", 5),
        }

    async def poll_connection(self, user_id: str, flow_data: dict[str, Any]) -> bool:
        """Poll GitHub token endpoint until authorized or timeout."""
        cid = os.getenv("GITHUB_CLIENT_ID", "").strip()
        csec = os.getenv("GITHUB_CLIENT_SECRET", "").strip()
        device_code = flow_data["device_code"]
        interval = flow_data.get("interval", 5)
        expires_in = flow_data.get("expires_in", 900)

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
                headers={"Accept": "application/json"},
                timeout=30,
            )
            tdata = resp.json()

            if "access_token" in tdata:
                expires_at = time.time() + tdata.get("expires_in", 3600)
                token_data: dict[str, Any] = {
                    "access_token": tdata["access_token"],
                    "expires_at": expires_at,
                }
                # Fetch username for display
                try:
                    user_resp = await asyncio.to_thread(
                        requests.get,
                        "https://api.github.com/user",
                        headers={
                            "Authorization": f"Bearer {tdata['access_token']}",
                            "Accept": "application/json",
                        },
                        timeout=10,
                    )
                    if user_resp.ok:
                        user_info = user_resp.json()
                        token_data["account"] = user_info.get("login", "")
                except Exception:
                    pass

                try:
                    token_store.write_token(user_id, "github", token_data)
                except TokenStoreError as exc:
                    raise RuntimeError(
                        f"GitHub OAuth succeeded but NALLY could not save the credential: {exc}"
                    ) from exc

                logger.info("GitHub token cached for user %s", user_id)
                return True

            error = tdata.get("error", "")
            if error == "authorization_pending":
                await asyncio.sleep(max(interval, 1))
            elif error == "slow_down":
                await asyncio.sleep(interval + 5)
            elif error == "expired_token":
                raise RuntimeError("GitHub device code expired. Please retry.")
            elif error == "access_denied":
                raise RuntimeError("GitHub OAuth access denied by user.")
            elif error:
                raise RuntimeError(f"GitHub OAuth unexpected response: {tdata}")
            else:
                raise RuntimeError(f"GitHub OAuth unexpected response: {tdata}")

        raise RuntimeError("GitHub OAuth device flow timed out.")

    def disconnect(self, user_id: str) -> bool:
        """Remove per-user cached token."""
        return token_store.clear_token(user_id, "github")

    def get_account_info(self, user_id: str) -> str | None:
        """Return GitHub username or None."""
        return token_store.get_account_info(user_id, "github")

    def get_auth_headers(self, user_id: str) -> dict[str, str] | None:
        """Return Authorization header for MCP HTTP transport."""
        token = token_store.get_valid_token(user_id, "github")
        if token:
            return {"Authorization": f"Bearer {token}"}
        pat = (
            os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "").strip()
            or os.getenv("GITHUB_TOKEN", "").strip()
        )
        if pat:
            return {"Authorization": f"Bearer {pat}"}
        return None

    def get_auth_env(self, user_id: str) -> dict[str, str] | None:
        """Return env vars for MCP stdio transport."""
        token = token_store.get_valid_token(user_id, "github")
        if token:
            return {"GITHUB_PERSONAL_ACCESS_TOKEN": token}
        pat = (
            os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "").strip()
            or os.getenv("GITHUB_TOKEN", "").strip()
        )
        if pat:
            return {"GITHUB_PERSONAL_ACCESS_TOKEN": pat}
        return None
