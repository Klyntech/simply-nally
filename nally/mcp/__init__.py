"""MCP — Model Context Protocol capability layer.

Thin client + auth + adapter. Agent never knows where tools came from.
"""

from .adapter import MCPTool, load_mcp_tools, load_mcp_tools_sync
from .auth import (
    DEFAULT_GMAIL_CACHE_FILE,
    AuthProvider,
    ChainedProvider,
    EnvTokenProvider,
    OAuthFileProvider,
    clear_gmail_token_cache,
    get_cached_token,
    get_gmail_cached_token,
    get_headers_for_server,
    gmail_token_is_valid,
    inject_auth,
    is_gmail_authenticated,
)
from .client import MCPClient

__all__ = [
    "AuthProvider",
    "ChainedProvider",
    "DEFAULT_GMAIL_CACHE_FILE",
    "EnvTokenProvider",
    "MCPClient",
    "MCPTool",
    "OAuthFileProvider",
    "clear_gmail_token_cache",
    "get_cached_token",
    "get_gmail_cached_token",
    "get_headers_for_server",
    "gmail_token_is_valid",
    "inject_auth",
    "is_gmail_authenticated",
    "load_mcp_tools",
    "load_mcp_tools_sync",
]
