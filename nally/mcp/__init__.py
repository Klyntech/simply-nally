"""MCP — Model Context Protocol transport layer.

Thin client + auth + adapter. Agent never knows where tools came from.
OAuth lifecycle lives in nally.integrations — this is pure transport.
"""

from .adapter import MCPTool, load_mcp_tools, load_mcp_tools_sync
from .auth import (
    SUPPORTED_MCP_SERVERS,
    AuthProvider,
    ChainedProvider,
    EnvTokenProvider,
    OAuthFileProvider,
    clear_token_cache,
    get_cached_token,
    get_headers_for_server,
    inject_auth,
)
from .client import MCPClient

__all__ = [
    "SUPPORTED_MCP_SERVERS",
    "AuthProvider",
    "ChainedProvider",
    "EnvTokenProvider",
    "MCPClient",
    "MCPTool",
    "OAuthFileProvider",
    "clear_token_cache",
    "get_cached_token",
    "get_headers_for_server",
    "inject_auth",
    "load_mcp_tools",
    "load_mcp_tools_sync",
]
