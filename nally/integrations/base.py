"""Base provider interface for MCP integrations.

Each provider (GitHub, Gmail, Notion) implements its own OAuth flow
behind this uniform interface. IntegrationManager uses this interface
without knowing flow details.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseProvider(ABC):
    """Uniform interface for integration providers.

    Subclasses implement their own OAuth flow. IntegrationManager
    calls these methods without knowing whether it's device flow,
    PKCE, or something else.
    """

    PROVIDER_NAME: str = ""

    @abstractmethod
    def is_connected(self, user_id: str) -> bool:
        """Check if user has valid credentials for this provider."""
        ...

    @abstractmethod
    async def connect(self, user_id: str) -> dict[str, Any]:
        """Start OAuth flow. Returns flow-specific data.

        For device flow: {user_code, verification_uri, device_code, expires_in, interval}
        For PKCE: {auth_url, ...state...}
        For other: provider-specific dict
        """
        ...

    @abstractmethod
    async def poll_connection(self, user_id: str, flow_data: dict[str, Any]) -> bool:
        """Poll for OAuth completion. Returns True when connected.

        For device flow: polls token endpoint until authorized.
        For PKCE: waits for callback server to receive code.
        """
        ...

    @abstractmethod
    def disconnect(self, user_id: str) -> bool:
        """Remove user's credentials. Returns True if was connected."""
        ...

    @abstractmethod
    def get_account_info(self, user_id: str) -> str | None:
        """Return account display name (email, username, etc.) or None."""
        ...

    @abstractmethod
    def get_auth_headers(self, user_id: str) -> dict[str, str] | None:
        """Return auth headers for MCP HTTP transport, or None."""
        ...

    @abstractmethod
    def get_auth_env(self, user_id: str) -> dict[str, str] | None:
        """Return auth env vars for MCP stdio transport, or None."""
        ...
