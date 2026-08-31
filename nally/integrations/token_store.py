"""Per-user credential storage.

Storage layout:
    ~/.config/simply-nally/tokens/{user_id}/{provider}.json

Format:
    {
        "access_token": "...",
        "expires_at": 1725000000.0,
        "refresh_token": "...",  // optional
        "account": "user@gmail.com"  // optional display name
    }

Providers do NOT know about this module. IntegrationManager owns
the TokenStore interface. Swap to encrypted storage by replacing
this module's internals.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BASE_DIR = Path("~/.config/simply-nally/tokens").expanduser()


def _user_dir(user_id: str) -> Path:
    """Return directory for a user's tokens."""
    return _BASE_DIR / user_id


def _token_file(user_id: str, provider: str) -> Path:
    """Return path to a specific provider token file."""
    return _user_dir(user_id) / f"{provider}.json"


def read_token(user_id: str, provider: str) -> dict[str, Any] | None:
    """Read raw token data. Returns None on missing/invalid."""
    p = _token_file(user_id, provider)
    try:
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("access_token"):
            return data
        logger.warning("Token file at %s has unexpected shape: %r", p, data)
        return None
    except json.JSONDecodeError as exc:
        logger.warning("Token file at %s is corrupt JSON: %s", p, exc)
        return None
    except OSError as exc:
        logger.warning("Cannot read token file at %s: %s", p, exc)
        return None


def write_token(user_id: str, provider: str, data: dict[str, Any]) -> None:
    """Write token data atomically with restrictive permissions."""
    p = _token_file(user_id, provider)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        with contextlib.suppress(Exception):
            os.chmod(tmp, 0o600)
        tmp.replace(p)
        with contextlib.suppress(Exception):
            os.chmod(p, 0o600)
    except OSError as exc:
        logger.warning("Cannot write token file at %s: %s", p, exc)
    except Exception as exc:
        logger.warning("Unexpected error writing token %s/%s: %s", user_id, provider, exc)


def clear_token(user_id: str, provider: str) -> bool:
    """Remove token file. Returns True if removed."""
    p = _token_file(user_id, provider)
    try:
        if p.exists():
            p.unlink()
            return True
        return False
    except OSError as exc:
        logger.warning("Cannot clear token file at %s: %s", p, exc)
        return False


def get_valid_token(user_id: str, provider: str) -> str | None:
    """Return access_token if valid (not expired), else None."""
    data = read_token(user_id, provider)
    if not data:
        return None
    expires_at = data.get("expires_at", 0)
    try:
        if time.time() >= float(expires_at):
            logger.debug("Token expired for %s/%s at %s", user_id, provider, expires_at)
            return None
    except (TypeError, ValueError):
        return None
    token = data.get("access_token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def token_is_valid(user_id: str, provider: str) -> bool:
    """Check if user has a valid (non-expired) token for a provider."""
    return get_valid_token(user_id, provider) is not None


def get_account_info(user_id: str, provider: str) -> str | None:
    """Return stored account display name, or None."""
    data = read_token(user_id, provider)
    if not data:
        return None
    account = data.get("account")
    if isinstance(account, str) and account.strip():
        return account.strip()
    return None


def clear_all_user_tokens(user_id: str) -> int:
    """Remove all tokens for a user. Returns count removed."""
    d = _user_dir(user_id)
    if not d.exists():
        return 0
    count = 0
    for f in d.glob("*.json"):
        try:
            f.unlink()
            count += 1
        except OSError:
            pass
    return count
