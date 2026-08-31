"""Compatibility shim — MCP adapter now lives in nally.mcp.adapter.

Prefer::

    from nally.mcp.adapter import MCPTool, load_mcp_tools

This module re-exports for backwards compatibility (agent.py still imports here).
"""

from nally.mcp.adapter import (  # noqa: F401
    MCPTool,
    _call_tool_async,
    _flatten_mcp_result,
    _has_mcp,
    _load_one_server,
    _mcp_schema_to_params,
    _run_async,
    load_mcp_tools,
    load_mcp_tools_sync,
)

__all__ = ["MCPTool", "load_mcp_tools", "load_mcp_tools_sync"]
