"""Tools package — registry construction + all built-in tools."""

from __future__ import annotations

import logging
from pathlib import Path

from .base import Tool, ToolRegistry, ToolResult
from .fetch import register_fetch_tools
from .filesystem import register_filesystem_tools
from .memory import register_memory_tools
from .shell import register_shell_tools
from .websearch import register_web_tools

logger = logging.getLogger(__name__)

__all__ = ["Tool", "ToolRegistry", "ToolResult", "build_default_registry"]


def build_default_registry(
    max_output: int = 8000,
    mcp_config: dict | None = None,
    load_mcp: bool = True,
    workspace: Path | None = None,
    user_id: str | None = None,
) -> ToolRegistry:
    """Create a registry with all built-in tools (+ MCP when enabled).

    MCP tools are discovered via ``nally.mcp`` and injected as normalized
    ``Tool`` objects. Agent never knows whether a tool came from
    ``filesystem.py`` or an MCP server.

    Memory tools are only registered when user_id is provided (persistence enabled).
    """
    registry = ToolRegistry(max_output=max_output)
    register_filesystem_tools(registry, workspace=workspace)
    register_shell_tools(registry)
    register_web_tools(registry)
    register_fetch_tools(registry)
    if user_id:
        try:
            register_memory_tools(registry, user_id)
        except Exception as exc:
            logger.warning("Memory tools not loaded: %s", exc)
    if load_mcp:
        try:
            from ..config import MCP_ENABLED, get_mcp_servers_config

            if MCP_ENABLED:
                cfg = mcp_config if mcp_config is not None else get_mcp_servers_config()
                if cfg:
                    try:
                        from ..mcp.adapter import load_mcp_tools_sync

                        load_mcp_tools_sync(registry, config=cfg)
                    except Exception as exc:
                        logger.warning(
                            "MCP tools not loaded: %s: %s", type(exc).__name__, exc
                        )
        except Exception as exc:
            logger.warning(
                "MCP setup failed: %s: %s", type(exc).__name__, exc
            )
    return registry
