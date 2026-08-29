"""GitHub OAuth device flow for MCP authentication.

Usage:
    from nally.github_oauth import get_github_token
    token = await get_github_token()
"""

from __future__ import annotations

import json
import os
import time

import requests

GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"

TOKEN_CACHE_FILE = os.path.expanduser("~/.config/simply-nally/github_oauth_token.json")


def _read_token_cache() -> dict[str, str] | None:
    try:
        with open(TOKEN_CACHE_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("access_token"):
            return data
    except Exception:
        pass
    return None


def _write_token_cache(token: str, expires_at: float) -> None:
    try:
        os.makedirs(os.path.dirname(TOKEN_CACHE_FILE), exist_ok=True)
        with open(TOKEN_CACHE_FILE, "w") as f:
            json.dump({"access_token": token, "expires_at": expires_at}, f)
    except Exception:
        pass


def _token_is_valid() -> bool:
    cache = _read_token_cache()
    if not cache:
        return False
    expires_at = cache.get("expires_at", 0)
    return time.time() < expires_at


def get_cached_token() -> str | None:
    if _token_is_valid():
        return _read_token_cache()["access_token"]
    return None


def is_github_authenticated() -> bool:
    """Check if GitHub MCP auth is available (PAT or cached device-flow token)."""
    pat = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "").strip() or os.getenv(
        "GITHUB_TOKEN", ""
    ).strip()
    if pat:
        return True
    try:
        return get_cached_token() is not None
    except Exception:
        return False


def clear_github_token() -> bool:
    """Remove cached GitHub device-flow token. Returns True if cleared."""
    try:
        if os.path.exists(TOKEN_CACHE_FILE):
            os.remove(TOKEN_CACHE_FILE)
            return True
    except Exception:
        pass
    return False


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
    """Poll for GitHub access token. Blocking — run in thread."""
    cid = (client_id or os.getenv("GITHUB_CLIENT_ID", "")).strip()
    csec = (client_secret or os.getenv("GITHUB_CLIENT_SECRET", "")).strip()
    deadline = time.time() + expires_in
    while time.time() < deadline:
        token_resp = requests.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id": cid,
                "client_secret": csec,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json"},
            timeout=30,
        )
        token_resp.raise_for_status()
        tdata = token_resp.json()

        if "access_token" in tdata:
            token = tdata["access_token"]
            expires_at = time.time() + tdata.get("expires_in", 3600)
            _write_token_cache(token, expires_at)
            return token

        error = tdata.get("error", "")
        if error == "authorization_pending":
            time.sleep(max(interval, 1))
        elif error == "slow_down":
            time.sleep(interval + 5)
        elif error == "expired_token":
            raise RuntimeError("GitHub device code expired. Please retry.")
        elif error == "access_denied":
            raise RuntimeError("GitHub OAuth access denied by user.")
        else:
            raise RuntimeError(f"GitHub OAuth unexpected response: {tdata}")

    raise RuntimeError("GitHub OAuth device flow timed out.")


async def get_github_token(
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    scopes: str | None = None,
    timeout: int = 120,
) -> str:
    """Get a GitHub access token via device flow. Blocks until user authorizes."""
    cid = (client_id or os.getenv("GITHUB_CLIENT_ID", "")).strip()
    csec = (client_secret or os.getenv("GITHUB_CLIENT_SECRET", "")).strip()
    if not cid or not csec:
        raise RuntimeError(
            "GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET not set. "
            "Register an OAuth App at https://github.com/settings/developers"
        )

    if _token_is_valid():
        return _read_token_cache()["access_token"]

    device_scopes = (
        scopes
        or os.getenv("GITHUB_OAUTH_SCOPES", "repo,read:user,workflow,pull_requests,issues").strip()
    )

    resp = requests.post(
        GITHUB_DEVICE_CODE_URL,
        data={
            "client_id": cid,
            "scope": device_scopes,
        },
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

    print(f"\nGitHub OAuth: visit {verification_uri} and enter code: {user_code}")
    print(f"Waiting for authorization (expires in {expires_in}s)...\n")

    deadline = time.time() + expires_in
    while time.time() < deadline:
        token_resp = requests.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id": cid,
                "client_secret": csec,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json"},
            timeout=30,
        )
        token_resp.raise_for_status()
        tdata = token_resp.json()

        if "access_token" in tdata:
            token = tdata["access_token"]
            expires_at = time.time() + tdata.get("expires_in", 3600)
            _write_token_cache(token, expires_at)
            print("GitHub OAuth: token obtained.\n")
            return token

        error = tdata.get("error", "")
        if error == "authorization_pending":
            time.sleep(max(interval, 1))
        elif error == "slow_down":
            time.sleep(interval + 5)
        elif error == "expired_token":
            raise RuntimeError("GitHub device code expired. Please retry.")
        elif error == "access_denied":
            raise RuntimeError("GitHub OAuth access denied by user.")
        else:
            raise RuntimeError(f"GitHub OAuth unexpected response: {tdata}")

    raise RuntimeError("GitHub OAuth device flow timed out.")
