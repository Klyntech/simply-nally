"""DEPRECATED — legacy OAuth integration layer.

This package is kept only for a short migration grace period.
Canonical path (v2):

    AuthBroker (nally.auth_broker)  →  CredentialVault (nally.vault)
                                    →  MCPConnectionBroker (nally.mcp.broker)

Do not add new code here. Prefer AuthBroker + Vault for all new work.
Will be removed in a future release.
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
    "NotionProvider",
    "IntegrationManager",
    "TokenStoreError",
    "clear_all_user_tokens",
    "clear_token",
    "get_account_info",
    "get_valid_token",
    "token_is_valid",
]
