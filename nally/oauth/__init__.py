"""OAuth subsystem — canonical credential management for NALLY.

This module owns:
- Token persistence (durable credentials per user/provider)
- OAuth flow state (temporary authorization state)
- Provider protocol (uniform interface for GitHub, Google, Notion)

MCP layer consumes credentials via OAuthManager.
Telegram initiates OAuth flows via OAuthManager.
Providers implement OAuth-specific mechanics behind the protocol.
"""

from .flow_store import OAuthFlow, OAuthFlowStore
from .manager import OAuthManager, get_oauth_manager
from .models import OAuthResult, OAuthSession, OAuthToken, ToolStatus
from .token_store import TokenStore

__all__ = [
    "OAuthFlow",
    "OAuthFlowStore",
    "OAuthManager",
    "OAuthResult",
    "OAuthSession",
    "OAuthToken",
    "TokenStore",
    "ToolStatus",
    "get_oauth_manager",
]
