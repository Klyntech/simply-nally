"""IntegrationManager — single entry point for MCP integration lifecycle.

Owns connection state. MCP layer only answers: given a credential,
can I connect and expose tools?

Telegram never touches OAuth directly.
MCP never touches user authentication directly.
IntegrationManager sits between them.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import BaseProvider

logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = {"github", "gmail", "notion"}


class IntegrationManager:
    """Manages OAuth connection lifecycle for MCP integrations.

    Usage from Telegram:
        manager = IntegrationManager()
        status = manager.status(user_id)
        await manager.connect(user_id, "github")
        await manager.poll_connection(user_id, "github", flow_data)

    Usage from MCP adapter:
        headers = manager.get_auth_headers(user_id, "github")
    """

    def __init__(self) -> None:
        self._providers: dict[str, BaseProvider] = {}
        self._register_providers()

    def _register_providers(self) -> None:
        """Lazily import and register providers."""
        from .github import GitHubProvider
        from .gmail import GmailProvider
        from .notion import NotionProvider

        self._providers = {
            "github": GitHubProvider(),
            "gmail": GmailProvider(),
            "notion": NotionProvider(),
        }

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self, user_id: str) -> dict[str, dict[str, Any]]:
        """Return status for all providers for a user.

        Returns:
            {
                "github": {"connected": True, "account": "username"},
                "gmail": {"connected": False, "account": None},
                "notion": {"connected": True, "account": "workspace"},
            }
        """
        result: dict[str, dict[str, Any]] = {}
        for name, provider in self._providers.items():
            try:
                result[name] = {
                    "connected": provider.is_connected(user_id),
                    "account": provider.get_account_info(user_id),
                }
            except Exception as exc:
                logger.warning("Status check failed for %s: %s", name, exc)
                result[name] = {"connected": False, "account": None}
        return result

    def is_connected(self, user_id: str, provider: str) -> bool:
        """Check if a specific provider is connected for a user."""
        self._validate_provider(provider)
        return self._providers[provider].is_connected(user_id)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, user_id: str, provider: str) -> dict[str, Any]:
        """Start OAuth flow for a provider.

        Returns flow-specific data:
            Device flow: {user_code, verification_uri, device_code, expires_in, interval}
            PKCE: {auth_url, ...state...}
        """
        self._validate_provider(provider)
        return await self._providers[provider].connect(user_id)

    async def poll_connection(self, user_id: str, provider: str, flow_data: dict[str, Any]) -> bool:
        """Poll for OAuth completion. Returns True when connected."""
        self._validate_provider(provider)
        return await self._providers[provider].poll_connection(user_id, flow_data)

    def disconnect(self, user_id: str, provider: str) -> bool:
        """Disconnect a provider. Returns True if was connected."""
        self._validate_provider(provider)
        return self._providers[provider].disconnect(user_id)

    def disconnect_all(self, user_id: str) -> int:
        """Disconnect all providers. Returns count of disconnected providers."""
        count = 0
        for provider in self._providers.values():
            try:
                if provider.disconnect(user_id):
                    count += 1
            except Exception as exc:
                logger.warning("Disconnect failed: %s", exc)
        return count

    # ------------------------------------------------------------------
    # Auth for MCP
    # ------------------------------------------------------------------

    def get_auth_headers(self, user_id: str, provider: str) -> dict[str, str] | None:
        """Return auth headers for MCP HTTP transport, or None."""
        self._validate_provider(provider)
        return self._providers[provider].get_auth_headers(user_id)

    def get_auth_env(self, user_id: str, provider: str) -> dict[str, str] | None:
        """Return auth env vars for MCP stdio transport, or None."""
        self._validate_provider(provider)
        return self._providers[provider].get_auth_env(user_id)

    # ------------------------------------------------------------------
    # Provider enumeration
    # ------------------------------------------------------------------

    def list_providers(self) -> list[str]:
        """Return list of supported provider names."""
        return list(self._providers.keys())

    def get_provider(self, name: str) -> BaseProvider:
        """Return provider instance by name."""
        self._validate_provider(name)
        return self._providers[name]

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_provider(self, provider: str) -> None:
        if provider not in self._providers:
            raise ValueError(
                f"Unknown provider: {provider}. "
                f"Supported: {', '.join(sorted(self._providers.keys()))}"
            )
