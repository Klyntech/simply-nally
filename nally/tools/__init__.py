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
    mcp_user_id: str | None = None,
) -> ToolRegistry:
    """Create a registry with all built-in tools (+ MCP when enabled).

    MCP tools are discovered via ``nally.mcp`` and injected as normalized
    ``Tool`` objects. Agent never knows whether a tool came from
    ``filesystem.py`` or an MCP server.

    If user_id is provided, per-user MCP auth is used via IntegrationManager.
    mcp_user_id overrides user_id for MCP token lookup (use Telegram user ID).

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
                        # v2: per-user broker cache (if user scoped) else direct load
                        # Credentials are stored under the *internal* user UUID (directory).
                        # Telegram passes raw telegram_id as mcp_user_id — resolve it.
                        lookup = mcp_user_id or user_id
                        if lookup:
                            try:
                                from ..directory import get_directory

                                d = get_directory()
                                # Prefer telegram mapping when mcp_user_id looks like a telegram id
                                u = None
                                if mcp_user_id:
                                    u = d.get_or_create_for_telegram(telegram_id=str(mcp_user_id))
                                if u and u.get("id"):
                                    lookup = u["id"]
                                    logger.debug("MCP lookup resolved telegram→internal %s", lookup[:8])
                            except Exception as exc:
                                logger.debug("Directory resolve for MCP skipped: %s", exc)
                        if lookup:
                            try:
                                from ..mcp.broker import get_broker

                                get_broker().get_tools_sync(lookup, registry=registry, config=cfg)
                            except Exception as exc:
                                logger.warning("MCP broker load failed: %s: %s", type(exc).__name__, exc)
                                # Fallback to direct adapter
                                from ..mcp.adapter import load_mcp_tools_sync

                                load_mcp_tools_sync(registry, config=cfg, user_id=lookup)
                        else:
                            from ..mcp.adapter import load_mcp_tools_sync

                            load_mcp_tools_sync(registry, config=cfg, user_id=None)
                    except Exception as exc:
                        logger.warning("MCP tools not loaded: %s: %s", type(exc).__name__, exc)
                    else:
                        mcp_count = sum(1 for n in registry.names() if n.startswith("mcp_"))
                        logger.info("MCP tools loaded: %d (user=%s)", mcp_count, (lookup or "cli")[:12])
                        if mcp_count == 0 and lookup:
                            logger.warning(
                                "0 MCP tools for user %s — check vault credential and NALLY_MCP_ENABLED",
                                lookup[:12],
                            )
        except Exception as exc:
            logger.warning("MCP setup failed: %s: %s", type(exc).__name__, exc)
    return registry
