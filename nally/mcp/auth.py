"""MCP authentication — AuthProvider abstraction.

Responsibility: resolve credentials for MCP servers.
Decoupled from transport (client.py) and discovery (adapter.py).

Providers:
    • EnvTokenProvider — PAT / token from environment
    • OAuthFileProvider — cached OAuth token from disk (GitHub)
    • ChainedProvider — tries multiple providers in order
    • no-op — returns None (unauthenticated)

Usage:
    from nally.mcp.auth import get_headers_for_server

    headers = get_headers_for_server("github", config)
    # -> {"Authorization": "Bearer ..."} or None

Config is not mutated; caller merges returned headers into a copy
before passing to MCPClient.

Token cache handling lives here so github_oauth.py can delegate to it
instead of duplicating file I/O.
"""

from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token cache — single place for file handling
# ---------------------------------------------------------------------------
DEFAULT_CACHE_FILE = os.path.expanduser("~/.config/simply-nally/github_oauth_token.json")
DEFAULT_GMAIL_CACHE_FILE = os.path.expanduser("~/.config/simply-nally/gmail_oauth_token.json")
DEFAULT_NOTION_CACHE_FILE = os.path.expanduser("~/.config/simply-nally/notion_oauth_token.json")


def _cache_path(path: str | None = None) -> Path:
    return Path(path or DEFAULT_CACHE_FILE).expanduser()


def read_token_cache(cache_file: str | None = None) -> dict[str, Any] | None:
    """Read cached token JSON. Returns None on missing/invalid."""
    p = _cache_path(cache_file)
    try:
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("access_token"):
            return data
        logger.warning("Token cache at %s has unexpected shape: %r", p, data)
        return None
    except json.JSONDecodeError as exc:
        logger.warning("Token cache at %s is corrupt JSON: %s", p, exc)
        return None
    except OSError as exc:
        logger.warning("Cannot read token cache at %s: %s", p, exc)
        return None
    except Exception as exc:  # pragma: no cover
        logger.warning("Unexpected error reading token cache %s: %s", p, exc)
        return None


def write_token_cache(
    token: str, expires_at: float, cache_file: str | None = None
) -> None:
    """Write token to cache file. Logs on failure, never raises."""
    p = _cache_path(cache_file)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"access_token": token, "expires_at": expires_at}),
            encoding="utf-8",
        )
        # Restrict permissions — token is plaintext (documented limitation)
        import contextlib

        with contextlib.suppress(Exception):
            os.chmod(tmp, 0o600)
        tmp.replace(p)
        with contextlib.suppress(Exception):
            os.chmod(p, 0o600)
    except OSError as exc:
        logger.warning("Cannot write token cache at %s: %s", p, exc)
    except Exception as exc:  # pragma: no cover
        logger.warning("Unexpected error writing token cache %s: %s", p, exc)


def clear_token_cache(cache_file: str | None = None) -> bool:
    """Remove cache file. Returns True if file was removed."""
    p = _cache_path(cache_file)
    try:
        if p.exists():
            p.unlink()
            return True
    except OSError as exc:
        logger.warning("Cannot clear token cache at %s: %s", p, exc)
    except Exception as exc:  # pragma: no cover
        logger.warning("Unexpected error clearing token cache %s: %s", p, exc)
    return False


def get_cached_token(cache_file: str | None = None) -> str | None:
    """Return cached access_token if valid (not expired), else None."""
    data = read_token_cache(cache_file)
    if not data:
        return None
    expires_at = data.get("expires_at", 0)
    try:
        if time.time() >= float(expires_at):
            logger.debug("Cached token expired at %s", expires_at)
            return None
    except (TypeError, ValueError):
        return None
    token = data.get("access_token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def token_is_valid(cache_file: str | None = None) -> bool:
    """Check if cache contains a non-expired token."""
    return get_cached_token(cache_file) is not None


# ---------------------------------------------------------------------------
# AuthProvider abstraction
# ---------------------------------------------------------------------------
class AuthProvider(ABC):
    """Returns headers for an MCP server config, or None."""

    @abstractmethod
    def get_headers(
        self, server_name: str, config: dict[str, Any]
    ) -> dict[str, str] | None:
        ...

    def is_authenticated(self, server_name: str, config: dict[str, Any]) -> bool:
        return self.get_headers(server_name, config) is not None


class EnvTokenProvider(AuthProvider):
    """PAT / bearer token from environment.

    Checks a list of env var names in order; first non-empty wins.

    Example:
        EnvTokenProvider(
            server_filter="github",
            env_vars=["GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_TOKEN"],
        )
    """

    def __init__(
        self,
        env_vars: list[str],
        header_name: str = "Authorization",
        prefix: str = "Bearer ",
        server_filter: str | None = None,
    ) -> None:
        self.env_vars = list(env_vars)
        self.header_name = header_name
        self.prefix = prefix
        self.server_filter = server_filter

    def get_headers(
        self, server_name: str, config: dict[str, Any]
    ) -> dict[str, str] | None:
        if self.server_filter and server_name != self.server_filter:
            return None
        for env_name in self.env_vars:
            token = os.getenv(env_name, "").strip()
            if token:
                return {self.header_name: f"{self.prefix}{token}"}
        return None


class OAuthFileProvider(AuthProvider):
    """OAuth token from file cache.

    Example:
        OAuthFileProvider(server_filter="github")
    """

    def __init__(
        self,
        cache_file: str | None = None,
        header_name: str = "Authorization",
        prefix: str = "Bearer ",
        server_filter: str | None = None,
    ) -> None:
        self.cache_file = cache_file
        self.header_name = header_name
        self.prefix = prefix
        self.server_filter = server_filter

    def get_headers(
        self, server_name: str, config: dict[str, Any]
    ) -> dict[str, str] | None:
        if self.server_filter and server_name != self.server_filter:
            return None
        token = get_cached_token(self.cache_file)
        if token:
            return {self.header_name: f"{self.prefix}{token}"}
        return None


class ChainedProvider(AuthProvider):
    """Try providers in order, first hit wins."""

    def __init__(self, providers: list[AuthProvider]) -> None:
        self.providers = list(providers)

    def get_headers(
        self, server_name: str, config: dict[str, Any]
    ) -> dict[str, str] | None:
        for p in self.providers:
            headers = p.get_headers(server_name, config)
            if headers:
                return headers
        return None


# ---------------------------------------------------------------------------
# Pre-built providers for Simply NALLY's known servers
# ---------------------------------------------------------------------------
def _github_provider() -> AuthProvider:
    return ChainedProvider(
        [
            EnvTokenProvider(
                env_vars=["GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_TOKEN"],
                server_filter="github",
            ),
            OAuthFileProvider(server_filter="github"),
        ]
    )


def _notion_provider() -> AuthProvider:
    """Notion: NOTION_TOKEN env first, then cached Notion OAuth file."""
    return ChainedProvider(
        [
            EnvTokenProvider(
                env_vars=["NOTION_TOKEN"],
                server_filter="notion",
            ),
            OAuthFileProvider(
                cache_file=DEFAULT_NOTION_CACHE_FILE,
                server_filter="notion",
            ),
        ]
    )


def _gmail_provider() -> AuthProvider:
    """Gmail: GMAIL_TOKEN env first, then cached Gmail OAuth file (Google official MCP)."""
    return ChainedProvider(
        [
            EnvTokenProvider(
                env_vars=["GMAIL_TOKEN", "GMAIL_OAUTH_TOKEN", "GOOGLE_GMAIL_TOKEN"],
                server_filter="gmail",
            ),
            OAuthFileProvider(
                cache_file=DEFAULT_GMAIL_CACHE_FILE,
                server_filter="gmail",
            ),
        ]
    )


_DEFAULT_PROVIDERS: dict[str, AuthProvider] = {
    "github": _github_provider(),
    "notion": _notion_provider(),
    "gmail": _gmail_provider(),
}


def _resolve_raw_token(server_name: str) -> str | None:
    """Return raw bearer token for server, or None.

    Checks PAT env first, then cached OAuth (for GitHub).
    For generic servers checks {SERVER}_TOKEN / {SERVER}_PAT.
    """
    # Known servers via providers — extract token from header value
    provider = _DEFAULT_PROVIDERS.get(server_name)
    if provider:
        # Use get_headers to avoid duplicating env logic, then strip prefix
        headers = provider.get_headers(server_name, {})
        if headers:
            # Expect {"Authorization": "Bearer <token>"}
            for v in headers.values():
                if isinstance(v, str) and v.startswith("Bearer "):
                    return v[len("Bearer ") :]
                if isinstance(v, str) and v:
                    return v
            return None
        return None

    # Generic fallback
    for env_name in (f"{server_name.upper()}_TOKEN", f"{server_name.upper()}_PAT"):
        token = os.getenv(env_name, "").strip()
        if token:
            return token
    return None


def get_headers_for_server(
    server_name: str, config: dict[str, Any] | None = None
) -> dict[str, str] | None:
    """Return auth headers for a given server, or None.

    Checks default providers (github PAT/OAuth, notion PAT).
    For unknown servers, tries a generic PAT from
    ``{SERVER}_TOKEN`` env var, else returns None.
    Explicit ``config.headers`` wins — returns None (no injection).
    """
    config = config or {}

    # If config already has headers, don't inject (explicit wins)
    if config.get("headers"):
        return None

    # Only for HTTP transports — stdio uses env injection (handled in inject_auth)
    # But we still allow header injection for stdio if caller explicitly wants it;
    # inject_auth will decide based on transport.
    provider = _DEFAULT_PROVIDERS.get(server_name)
    if provider:
        return provider.get_headers(server_name, config)

    # Generic fallback: look for e.g. LINEAR_TOKEN, FIGMA_TOKEN
    generic_env = f"{server_name.upper()}_TOKEN"
    token = os.getenv(generic_env, "").strip()
    if token:
        return {"Authorization": f"Bearer {token}"}

    # Also try {SERVER}_PAT
    generic_pat = f"{server_name.upper()}_PAT"
    token = os.getenv(generic_pat, "").strip()
    if token:
        return {"Authorization": f"Bearer {token}"}

    return None


# Map server -> env var name for stdio transport
_STDIO_ENV_MAP: dict[str, str] = {
    "github": "GITHUB_PERSONAL_ACCESS_TOKEN",
    "notion": "NOTION_TOKEN",
    "gmail": "GMAIL_TOKEN",
}


# ---------------------------------------------------------------------------
# Gmail-specific helpers (thin wrappers over generic cache with gmail file)
# ---------------------------------------------------------------------------
def get_gmail_cached_token() -> str | None:
    """Return cached Gmail access_token if valid, else None."""
    return get_cached_token(DEFAULT_GMAIL_CACHE_FILE)


def gmail_token_is_valid() -> bool:
    """Check if Gmail cache contains a non-expired token."""
    return token_is_valid(DEFAULT_GMAIL_CACHE_FILE)


def is_gmail_authenticated() -> bool:
    """Check if Gmail is authenticated (env or cached file)."""
    provider = _DEFAULT_PROVIDERS.get("gmail")
    if provider and provider.get_headers("gmail", {}):
        return True
    return False


def clear_gmail_token_cache() -> bool:
    """Remove Gmail cache file. Returns True if removed."""
    return clear_token_cache(DEFAULT_GMAIL_CACHE_FILE)


# ---------------------------------------------------------------------------
# Notion-specific helpers (thin wrappers over generic cache with notion file)
# ---------------------------------------------------------------------------
def get_notion_cached_token() -> str | None:
    """Return cached Notion access_token if valid, else None."""
    return get_cached_token(DEFAULT_NOTION_CACHE_FILE)


def notion_token_is_valid() -> bool:
    """Check if Notion cache contains a non-expired token."""
    return token_is_valid(DEFAULT_NOTION_CACHE_FILE)


def is_notion_authenticated() -> bool:
    """Check if Notion is authenticated (env or cached file)."""
    provider = _DEFAULT_PROVIDERS.get("notion")
    return bool(provider and provider.get_headers("notion", {}))


def clear_notion_token_cache() -> bool:
    """Remove Notion cache file. Returns True if removed."""
    return clear_token_cache(DEFAULT_NOTION_CACHE_FILE)


def inject_auth(
    configs: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return a new configs dict with auth injected where needed.

    Does not mutate the input.
    • HTTP (``url``)  → injects ``headers: {Authorization: Bearer …}``
    • stdio (``command``) → injects ``env: {TOKEN_VAR: …}``

    Explicit ``headers`` / ``env`` entries are preserved (no injection
    if they already contain an equivalent credential).
    """
    result: dict[str, dict[str, Any]] = {}
    for name, cfg in configs.items():
        if not isinstance(cfg, dict):
            result[name] = cfg
            continue

        # stdio transport — inject env
        if cfg.get("command"):
            env = cfg.get("env")
            # If env already has a token key, don't inject
            env_has_token = False
            if isinstance(env, dict):
                token_key = _STDIO_ENV_MAP.get(name, f"{name.upper()}_TOKEN")
                if token_key in env or "GITHUB_TOKEN" in env or "NOTION_TOKEN" in env:
                    env_has_token = True
            if env_has_token:
                result[name] = dict(cfg)
                continue
            token = _resolve_raw_token(name)
            if token:
                token_key = _STDIO_ENV_MAP.get(name, f"{name.upper()}_TOKEN")
                new_env = dict(env) if isinstance(env, dict) else {}
                new_env[token_key] = token
                result[name] = {**cfg, "env": new_env}
            else:
                result[name] = dict(cfg)
            continue

        # HTTP transport — inject headers
        headers = get_headers_for_server(name, cfg)
        if headers:
            existing = cfg.get("headers") or {}
            merged = {**existing, **headers}
            result[name] = {**cfg, "headers": merged}
        else:
            result[name] = dict(cfg)
    return result


# ---------------------------------------------------------------------------
# Backwards compat — re-exported for github_oauth / config shims
# ---------------------------------------------------------------------------
# These keep github_oauth.py working while it migrates to this module.
__all__ = [
    "DEFAULT_CACHE_FILE",
    "DEFAULT_GMAIL_CACHE_FILE",
    "DEFAULT_NOTION_CACHE_FILE",
    "AuthProvider",
    "ChainedProvider",
    "EnvTokenProvider",
    "OAuthFileProvider",
    "clear_gmail_token_cache",
    "clear_notion_token_cache",
    "clear_token_cache",
    "get_cached_token",
    "get_gmail_cached_token",
    "get_headers_for_server",
    "get_notion_cached_token",
    "gmail_token_is_valid",
    "inject_auth",
    "is_gmail_authenticated",
    "is_notion_authenticated",
    "notion_token_is_valid",
    "read_token_cache",
    "token_is_valid",
    "write_token_cache",
]
