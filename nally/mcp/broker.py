"""MCPConnectionBroker — owns MCP server connections via vault credential reference.

Tool discovery cache is per-user, invalidated on connect/disconnect/refresh failure/
credential replacement. Transport never sees raw tokens beyond ephemeral header/env
injection.

This replaces ad-hoc `load_mcp_tools_sync` per-agent construction with a shared cache.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class UserToolCache:
    user_id: str
    tools: dict[str, Any]  # name -> MCPTool
    connected_providers: set[str]
    last_refresh: float = field(default_factory=time.monotonic)
    ttl_seconds: int = 300

    @property
    def is_expired(self) -> bool:
        return (time.monotonic() - self.last_refresh) > self.ttl_seconds


class MCPConnectionBroker:
    """Manages MCP connections with credential isolation and tool caching."""

    def __init__(self, vault=None):
        from nally.vault import get_vault

        self._vault = vault or get_vault()
        self._cache: dict[str, UserToolCache] = {}
        self._lock = asyncio.Lock()

    async def get_tools(
        self, user_id: str | None, registry=None, config: dict[str, Any] | None = None, timeout: float | None = None
    ) -> int:
        """Load tools for user, using cache if valid. Returns count."""
        # CLI/no user — no cache, direct load
        if not user_id:
            return await self._load_fresh(user_id, registry, config, timeout)
        # Check cache
        entry = self._cache.get(user_id)
        if entry and not entry.is_expired:
            if registry is not None:
                for tool in entry.tools.values():
                    try:
                        registry.register(tool)
                    except Exception:
                        pass
            return len(entry.tools)
        # Cache miss/expired — fresh load
        count = await self._load_fresh(user_id, registry, config, timeout, cache=True)
        return count

    async def _load_fresh(self, user_id: str | None, registry, config, timeout, cache: bool = False) -> int:
        from nally.mcp.adapter import load_mcp_tools

        if registry is None:
            from nally.tools.base import ToolRegistry

            registry = ToolRegistry()
        # load_mcp_tools now delegates to vault for user-scoped creds
        # Ensure config resolved
        if config is None:
            try:
                from nally.config import get_mcp_servers_config, MCP_TIMEOUT

                config = get_mcp_servers_config()
                if timeout is None:
                    timeout = float(MCP_TIMEOUT)
            except Exception:
                config = {}
                timeout = timeout or 30.0
        # For per-user, we need to load with user_id so vault is used
        total = await load_mcp_tools(registry, config=config, timeout=timeout, user_id=user_id)
        if cache and user_id:
            # Snapshot registry tools into cache
            tools = {name: tool for name, tool in registry._tools.items() if name.startswith("mcp_")}
            # Determine connected providers from vault
            try:
                providers = set(self._vault.list_providers(user_id))
            except Exception:
                providers = set()
            self._cache[user_id] = UserToolCache(
                user_id=user_id, tools=tools, connected_providers=providers
            )
        return total

    def get_tools_sync(self, user_id: str | None, registry=None, config=None, timeout=None) -> int:
        """Sync wrapper."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            import concurrent.futures

            fut: concurrent.futures.Future = asyncio.run_coroutine_threadsafe(
                self.get_tools(user_id, registry, config, timeout), loop
            )
            return fut.result(timeout=60)
        else:
            return asyncio.run(self.get_tools(user_id, registry, config, timeout))

    async def invalidate_cache(self, user_id: str, provider: str | None = None) -> None:
        """Invalidate cache for user (all or specific provider)."""
        if provider is None:
            removed = self._cache.pop(user_id, None)
            if removed:
                logger.info("mcp cache invalidated user=%s all", user_id[:8])
            return
        entry = self._cache.get(user_id)
        if entry is None:
            return
        # Remove tools for this provider and update connected set
        to_remove = [k for k in entry.tools.keys() if k.startswith(f"mcp_{provider}_")]
        for k in to_remove:
            entry.tools.pop(k, None)
        entry.connected_providers.discard(provider)
        # If no tools left, keep entry but mark refresh needed? Easier to expire
        entry.last_refresh = 0  # force refresh next get
        logger.info("mcp cache invalidated user=%s provider=%s removed=%d", user_id[:8], provider, len(to_remove))
        # If provider was the only one, we could keep empty but will reload

    def invalidate_sync(self, user_id: str, provider: str | None = None) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            import concurrent.futures

            fut = asyncio.run_coroutine_threadsafe(self.invalidate_cache(user_id, provider), loop)
            try:
                fut.result(timeout=5)
            except Exception:
                pass
        else:
            asyncio.run(self.invalidate_cache(user_id, provider))

    async def connect(self, user_id: str, provider: str) -> dict[str, Any]:
        """Connect is implicit via vault credential + cache invalidation."""
        # After vault put, we invalidate so next get_tools sees new provider
        await self.invalidate_cache(user_id, provider)
        # Optionally warm cache
        try:
            from nally.tools.base import ToolRegistry

            reg = ToolRegistry()
            await self.get_tools(user_id, registry=reg)
        except Exception as exc:
            logger.debug("mcp connect warm cache failed for %s: %s", provider, exc)
        return {"connected": True, "provider": provider}

    async def disconnect(self, user_id: str, provider: str) -> bool:
        """Disconnect: delete vault credential and invalidate cache."""
        try:
            from nally.auth_broker import get_broker

            broker = get_broker()
            ok = await broker.revoke(user_id, provider)
            await self.invalidate_cache(user_id, provider)
            return ok
        except Exception as exc:
            logger.warning("mcp disconnect failed for %s/%s: %s", user_id[:8], provider, exc)
            # Still try vault delete directly
            try:
                ok = self._vault.delete(user_id, provider)
                await self.invalidate_cache(user_id, provider)
                return ok
            except Exception:
                return False

    async def call_tool(
        self, user_id: str | None, tool_name: str, arguments: dict[str, Any], timeout: float = 30.0
    ) -> str:
        """Call tool via short-lived MCP session with vault credential injection.

        This is the runtime path for agent tool execution.
        """
        # Parse server and orig name from namespaced tool: mcp_{server}_{orig}
        # Known servers: github, gmail, notion
        if not tool_name.startswith("mcp_"):
            raise ValueError(f"Not an MCP tool: {tool_name}")
        rest = tool_name[len("mcp_"):]
        server_name = None
        orig_name = None
        for s in ("github", "gmail", "notion"):
            prefix = s + "_"
            if rest.startswith(prefix):
                server_name = s
                orig_name = rest[len(prefix):]
                break
        if not server_name or not orig_name:
            raise ValueError(f"Invalid MCP tool name: {tool_name}")
        from nally.mcp.adapter import _call_tool_async

        # Use vault-based auth via adapter's _call_tool_async which now checks vault
        return await _call_tool_async(
            server_name, await self._resolve_server_config(server_name, user_id), orig_name, arguments, timeout=timeout, user_id=user_id
        )

    async def _resolve_server_config(self, server_name: str, user_id: str | None) -> dict[str, Any]:
        from nally.config import get_mcp_servers_config

        cfg = get_mcp_servers_config()
        base = cfg.get(server_name, {})
        # For user-scoped, vault injection happens inside _call_tool_async via _inject_user_auth
        # Here we just return base transport config
        return dict(base)


# Singleton
_default_mcp_broker: MCPConnectionBroker | None = None


def get_broker() -> MCPConnectionBroker:
    global _default_mcp_broker
    if _default_mcp_broker is None:
        _default_mcp_broker = MCPConnectionBroker()
    return _default_mcp_broker


def reset_broker() -> None:
    global _default_mcp_broker
    _default_mcp_broker = None
