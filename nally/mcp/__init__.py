"""MCP — Model Context Protocol capability layer.

Thin client + auth + adapter. Agent never knows where tools came from.
"""

from .adapter import MCPTool, load_mcp_tools, load_mcp_tools_sync
from .auth import (
    DEFAULT_GMAIL_CACHE_FILE,
    DEFAULT_NOTION_CACHE_FILE,
    AuthProvider,
    ChainedProvider,
    EnvTokenProvider,
    OAuthFileProvider,
    clear_gmail_token_cache,
    clear_notion_token_cache,
    get_cached_token,
    get_gmail_cached_token,
    get_headers_for_server,
    get_notion_cached_token,
    gmail_token_is_valid,
    inject_auth,
    is_gmail_authenticated,
    is_notion_authenticated,
    notion_token_is_valid,
)
from .client import MCPClient

__all__ = [
    "DEFAULT_GMAIL_CACHE_FILE",
    "DEFAULT_NOTION_CACHE_FILE",
    "AuthProvider",
    "ChainedProvider",
    "EnvTokenProvider",
    "MCPClient",
    "MCPTool",
    "OAuthFileProvider",
    "clear_gmail_token_cache",
    "clear_notion_token_cache",
    "get_cached_token",
    "get_gmail_cached_token",
    "get_headers_for_server",
    "get_notion_cached_token",
    "gmail_token_is_valid",
    "inject_auth",
    "is_gmail_authenticated",
    "is_notion_authenticated",
    "load_mcp_tools",
    "load_mcp_tools_sync",
    "notion_token_is_valid",
]
