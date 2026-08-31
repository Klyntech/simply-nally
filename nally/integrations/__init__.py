"""Integrations — OAuth connection lifecycle for MCP providers.

Each provider (GitHub, Gmail, Notion) implements its own OAuth flow
behind a uniform BaseProvider interface. IntegrationManager gives
Telegram one clean interface without knowing flow details.

Usage:
    from nally.integrations import IntegrationManager

    manager = IntegrationManager()
    status = manager.status(user_id)
    await manager.connect(user_id, "github")
"""

from .base import BaseProvider
from .github import GitHubProvider
from .gmail import GmailProvider
from .manager import SUPPORTED_PROVIDERS, IntegrationManager
from .notion import NotionProvider
from .token_store import (
    TokenStoreError,
    clear_all_user_tokens,
    clear_token,
    get_account_info,
    get_valid_token,
    token_is_valid,
)

__all__ = [
    "SUPPORTED_PROVIDERS",
    "BaseProvider",
    "GitHubProvider",
    "GmailProvider",
    "IntegrationManager",
    "NotionProvider",
    "TokenStoreError",
    "clear_all_user_tokens",
    "clear_token",
    "get_account_info",
    "get_valid_token",
    "token_is_valid",
]
