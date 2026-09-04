"""MCP authentication — env-based AuthProvider (CLI / single-user only).

For multi-user / production the canonical path is:

    AuthBroker → CredentialVault → MCP adapter (_inject_user_auth)

This module is still used by the CLI fallback path when
NALLY_ALLOW_ENV_FALLBACK=true or when no user_id is present.

Supported servers: github, gmail, notion.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)

# Supported MCP servers — v1 lock
SUPPORTED_MCP_SERVERS: set[str] = {"github", "gmail", "notion"}


# ---------------------------------------------------------------------------
# AuthProvider abstraction
# ---------------------------------------------------------------------------
class AuthProvider(ABC):
    """Returns headers for an MCP server config, or None."""

    @abstractmethod
    def get_headers(self, server_name: str, config: dict[str, Any]) -> dict[str, str] | None: ...

    def is_authenticated(self, server_name: str, config: dict[str, Any]) -> bool:
        return self.get_headers(server_name, config) is not None


class EnvTokenProvider(AuthProvider):
    """PAT / bearer token from environment.

    Checks a list of env var names in order; first non-empty wins.
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

    def get_headers(self, server_name: str, config: dict[str, Any]) -> dict[str, str] | None:
        if self.server_filter and server_name != self.server_filter:
            return None
        for env_name in self.env_vars:
            token = os.getenv(env_name, "").strip()
            if token:
                return {self.header_name: f"{self.prefix}{token}"}
        return None


# ---------------------------------------------------------------------------
# Pre-built env-var providers for supported servers
# ---------------------------------------------------------------------------
_GITHUB_ENV_PROVIDER = EnvTokenProvider(
    env_vars=["GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_TOKEN"],
    server_filter="github",
)
_NOTION_ENV_PROVIDER = EnvTokenProvider(
    env_vars=["NOTION_TOKEN"],
    server_filter="notion",
)
_GMAIL_ENV_PROVIDER = EnvTokenProvider(
    env_vars=["GMAIL_TOKEN", "GMAIL_OAUTH_TOKEN", "GOOGLE_GMAIL_TOKEN"],
    server_filter="gmail",
)

_ENV_PROVIDERS: dict[str, EnvTokenProvider] = {
    "github": _GITHUB_ENV_PROVIDER,
    "notion": _NOTION_ENV_PROVIDER,
    "gmail": _GMAIL_ENV_PROVIDER,
}

# Map server -> env var name for stdio transport
_STDIO_ENV_MAP: dict[str, str] = {
    "github": "GITHUB_PERSONAL_ACCESS_TOKEN",
    "notion": "NOTION_TOKEN",
    "gmail": "GMAIL_TOKEN",
}


def _resolve_env_token(server_name: str) -> str | None:
    """Return raw bearer token from env vars for a server, or None."""
    provider = _ENV_PROVIDERS.get(server_name)
    if provider:
        headers = provider.get_headers(server_name, {})
        if headers:
            for v in headers.values():
                if isinstance(v, str) and v.startswith("Bearer "):
                    return v[len("Bearer ") :]
                if isinstance(v, str) and v:
                    return v
    return None


def get_headers_for_server(
    server_name: str, config: dict[str, Any] | None = None
) -> dict[str, str] | None:
    """Return auth headers for a given server, or None.

    Only resolves supported servers (github, gmail, notion).
    Explicit config.headers wins — returns None (no injection).
    """
    config = config or {}

    if config.get("headers"):
        return None

    provider = _ENV_PROVIDERS.get(server_name)
    if provider:
        return provider.get_headers(server_name, config)

    if server_name not in SUPPORTED_MCP_SERVERS:
        logger.debug("Ignoring auth for unsupported MCP server: %s", server_name)

    return None


def inject_auth(
    configs: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return a new configs dict with auth injected where needed.

    Does not mutate the input. Rejects unsupported servers.
    • HTTP (url)  → injects headers: {Authorization: Bearer ...}
    • stdio (command) → injects env: {TOKEN_VAR: ...}
    """
    result: dict[str, dict[str, Any]] = {}
    for name, cfg in configs.items():
        if not isinstance(cfg, dict):
            result[name] = cfg
            continue

        # Reject unsupported servers
        if name not in SUPPORTED_MCP_SERVERS:
            logger.warning("Rejecting unsupported MCP server: %s", name)
            continue

        # stdio transport — inject env
        if cfg.get("command"):
            env = cfg.get("env")
            env_has_token = False
            if isinstance(env, dict):
                token_key = _STDIO_ENV_MAP.get(name, f"{name.upper()}_TOKEN")
                if token_key in env or "GITHUB_TOKEN" in env or "NOTION_TOKEN" in env:
                    env_has_token = True
            if env_has_token:
                result[name] = dict(cfg)
                continue
            token = _resolve_env_token(name)
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


__all__ = [
    "SUPPORTED_MCP_SERVERS",
    "AuthProvider",
    "EnvTokenProvider",
    "get_headers_for_server",
    "inject_auth",
]
