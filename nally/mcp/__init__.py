"""MCP — Model Context Protocol capability layer.

Thin client + auth + adapter. Agent never knows where tools came from.
"""

from .adapter import MCPTool, load_mcp_tools, load_mcp_tools_sync
from .auth import (
    AuthProvider,
    ChainedProvider,
    EnvTokenProvider,
    OAuthFileProvider,
    get_cached_token,
    get_headers_for_server,
    inject_auth,
)
from .client import MCPClient

__all__ = [
    "AuthProvider",
    "ChainedProvider",
    "EnvTokenProvider",
    "MCPClient",
    "MCPTool",
    "OAuthFileProvider",
    "get_cached_token",
    "get_headers_for_server",
    "inject_auth",
    "load_mcp_tools",
    "load_mcp_tools_sync",
]
