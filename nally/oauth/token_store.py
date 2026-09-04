"""Token persistence — durable credential storage per user/provider.

Storage layout:
    ~/.config/simply-nally/tokens/{user_id}/{provider}.json

This is the ONLY location where OAuth tokens should be stored.
Legacy modules (github_oauth.py, notion_oauth.py) must not maintain
their own token caches.

Security invariant:
    user_id + provider → exactly one token
    Never fall back to global/environment credentials for user-scoped requests.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path

from .models import OAuthToken

logger = logging.getLogger(__name__)

_BASE_DIR = Path("~/.config/simply-nally/tokens").expanduser()


class TokenStoreError(Exception):
    """Raised when token storage operations fail."""


def _validate_user_id(user_id: str) -> None:
    """Validate user_id before using it as a filesystem path component."""
    if not user_id or not isinstance(user_id, str):
        raise TokenStoreError("user_id must be a non-empty string")
    if len(user_id) > 128:
        raise TokenStoreError(f"user_id too long: {len(user_id)} chars (max 128)")
    if not all(c.isalnum() or c in "_-" for c in user_id):
        raise TokenStoreError(f"user_id contains invalid characters: {user_id!r}")


def _validate_provider(provider: str) -> None:
    """Validate provider name."""
    if not provider or not isinstance(provider, str):
        raise TokenStoreError("provider must be a non-empty string")
    if not all(c.isalnum() or c in "_" for c in provider):
        raise TokenStoreError(f"provider contains invalid characters: {provider!r}")


def _user_dir(user_id: str) -> Path:
    """Return directory for a user's tokens."""
    return _BASE_DIR / user_id


def _token_file(user_id: str, provider: str) -> Path:
    """Return path to a specific provider token file."""
    return _user_dir(user_id) / f"{provider}.json"


class TokenStore:
    """Per-user, per-provider token storage.

    This is the single source of truth for OAuth credentials.
    All modules must use this class, not their own file-based caches.

    Usage:
        store = TokenStore()
        token = store.get(user_id="12345", provider="github")
        if token and not token.is_expired:
            use(token.access_token)
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = base_dir or _BASE_DIR

    def get(self, user_id: str, provider: str) -> OAuthToken | None:
        """Read token. Returns None if missing/invalid/expired."""
        _validate_user_id(user_id)
        _validate_provider(provider)
        p = _token_file(user_id, provider)
        try:
            if not p.exists():
                return None
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not data.get("access_token"):
                logger.warning("Token file at %s has unexpected shape: %r", p, data)
                return None
            token = OAuthToken.from_dict(data)
            return token
        except json.JSONDecodeError as exc:
            logger.warning("Token file at %s is corrupt JSON: %s", p, exc)
            return None
        except OSError as exc:
            logger.warning("Cannot read token file at %s: %s", p, exc)
            return None

    def get_valid(self, user_id: str, provider: str) -> OAuthToken | None:
        """Read token only if not expired. Returns None if expired or missing."""
        token = self.get(user_id, provider)
        if token is None:
            return None
        if token.is_expired:
            logger.debug("Token expired for %s/%s", user_id, provider)
            return None
        return token

    def put(self, user_id: str, provider: str, token: OAuthToken) -> None:
        """Write token atomically with restrictive permissions.

        Raises TokenStoreError on failure. Callers must handle this —
        a failed write means the OAuth provider succeeded but NALLY
        could not persist the credential.
        """
        _validate_user_id(user_id)
        _validate_provider(provider)
        p = _token_file(user_id, provider)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(token.to_dict()), encoding="utf-8")
            with contextlib.suppress(Exception):
                os.chmod(tmp, 0o600)
            tmp.replace(p)
            with contextlib.suppress(Exception):
                os.chmod(p, 0o600)
        except OSError as exc:
            raise TokenStoreError(f"Cannot write token for {user_id}/{provider}: {exc}") from exc
        except Exception as exc:
            raise TokenStoreError(
                f"Unexpected error writing token {user_id}/{provider}: {exc}"
            ) from exc

    def delete(self, user_id: str, provider: str) -> bool:
        """Remove token file. Returns True if removed."""
        _validate_user_id(user_id)
        _validate_provider(provider)
        p = _token_file(user_id, provider)
        try:
            if p.exists():
                p.unlink()
                return True
            return False
        except OSError as exc:
            logger.warning("Cannot delete token file at %s: %s", p, exc)
            return False

    def delete_all(self, user_id: str) -> int:
        """Remove all tokens for a user. Returns count removed."""
        _validate_user_id(user_id)
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

    def list_providers(self, user_id: str) -> list[str]:
        """Return list of providers with stored tokens for a user."""
        _validate_user_id(user_id)
        d = _user_dir(user_id)
        if not d.exists():
            return []
        return [f.stem for f in d.glob("*.json") if f.stem]

    def has_token(self, user_id: str, provider: str) -> bool:
        """Check if a token exists (may be expired)."""
        return self.get(user_id, provider) is not None

    def is_valid(self, user_id: str, provider: str) -> bool:
        """Check if a valid (non-expired) token exists."""
        return self.get_valid(user_id, provider) is not None
