"""MCP client — thin wrapper over mcp.ClientSession.

Minimal, testable boundary:

    MCPClient
        ├── connect()
        ├── list_tools()
        ├── call_tool()
        └── close()

Also usable as async context manager:

    async with MCPClient(config) as client:
        tools = await client.list_tools()
        result = await client.call_tool("github_get_file", {...})

Transport is derived from config:

    stdio:  {"command": "npx", "args": ["-y", "..."], "env": {...}}
    http:   {"url": "https://api.githubcopilot.com/mcp/", "headers": {...}}

Does not handle auth — caller is responsible for populating
headers/env via an AuthProvider.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _has_mcp() -> bool:
    try:
        import mcp  # noqa: F401

        return True
    except ImportError:
        return False


class MCPClient:
    """Thin, stateful wrapper around an MCP server connection.

    Args:
        config: Server config dict. Must contain either ``command`` (stdio)
            or ``url`` (Streamable HTTP). Optional: ``args``, ``env``,
            ``headers``.
        timeout: Seconds for HTTP transport. Also used for tool calls
            that go over HTTP.
    """

    def __init__(self, config: dict[str, Any], timeout: float = 30.0) -> None:
        if not isinstance(config, dict):
            raise TypeError("config must be a dict")
        self.config = config
        self.timeout = float(timeout)
        self._stack: contextlib.AsyncExitStack | None = None
        self._session: Any | None = None
        self._http_client: Any | None = None
        self._connected = False

    # ------------------------------------------------------------------ private
    def _validate_config(self) -> None:
        if not self.config.get("command") and not self.config.get("url"):
            raise ValueError("MCP server config must have 'command' or 'url'")

    # ------------------------------------------------------------------ connect
    async def connect(self) -> None:
        """Establish transport and initialise MCP session.

        Idempotent — calling twice is a no-op (second call returns immediately
        if already connected).
        """
        if self._connected:
            return
        if not _has_mcp():
            raise RuntimeError(
                "mcp package not installed. Install with: pip install \"simply-nally[mcp]\""
            )
        self._validate_config()

        from mcp.client.session import ClientSession

        command = self.config.get("command")
        url = self.config.get("url")
        headers = self.config.get("headers")
        env = self.config.get("env")
        args = self.config.get("args") or []

        stack = contextlib.AsyncExitStack()
        try:
            if command:
                from mcp.client.stdio import StdioServerParameters, stdio_client

                params = StdioServerParameters(
                    command=command,
                    args=list(args),
                    env=env,
                )
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                # Bound initialize by timeout so remote hangs don't block tests
                await asyncio.wait_for(session.initialize(), timeout=self.timeout)
                self._stack = stack
                self._session = session
                self._connected = True
                logger.debug("MCP connected (stdio): %s %s", command, args)

            elif url:
                from mcp.client.streamable_http import streamable_http_client

                http_client = None
                if headers:
                    try:
                        import httpx

                        http_client = httpx.AsyncClient(
                            headers=headers, timeout=self.timeout
                        )
                    except ImportError:
                        # httpx not installed — let mcp create its own client
                        logger.warning("httpx not installed, ignoring custom headers")
                        http_client = None

                # Keep reference so we can close it in close()
                self._http_client = http_client

                read, write, _get_sid = await stack.enter_async_context(
                    streamable_http_client(url, http_client=http_client)
                )
                session = await stack.enter_async_context(ClientSession(read, write))
                await asyncio.wait_for(session.initialize(), timeout=self.timeout)
                self._stack = stack
                self._session = session
                self._connected = True
                logger.debug("MCP connected (http): %s", url)

            else:
                # Should be unreachable due to _validate_config
                raise ValueError("MCP server config must have 'command' or 'url'")

        except BaseException:
            # If initialization failed part-way, unwind what we entered
            with contextlib.suppress(Exception):
                await stack.aclose()
            if self._http_client is not None:
                with contextlib.suppress(Exception):
                    await self._http_client.aclose()
                self._http_client = None
            self._stack = None
            self._session = None
            self._connected = False
            raise

    # ------------------------------------------------------------------ ops
    async def list_tools(self, cursor: str | None = None) -> Any:
        """List tools on the connected server.

        Must be called after :meth:`connect`. Handles pagination via
        ``cursor`` — caller is responsible for looping until
        ``result.nextCursor`` is falsy.
        """
        if not self._connected or self._session is None:
            raise RuntimeError("MCPClient not connected — call connect() first")
        return await self._session.list_tools(cursor=cursor)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call a tool on the connected server.

        Must be called after :meth:`connect`.
        Returns the raw ``CallToolResult`` (caller can flatten as needed).
        """
        if not self._connected or self._session is None:
            raise RuntimeError("MCPClient not connected — call connect() first")
        if not name:
            raise ValueError("tool name must be non-empty")
        return await self._session.call_tool(name, arguments or {})

    # ------------------------------------------------------------------ close
    async def close(self) -> None:
        """Close the session and underlying transport. Idempotent."""
        if not self._connected:
            # Still need to clean up http_client if connect failed mid-way
            if self._http_client is not None:
                with contextlib.suppress(Exception):
                    await self._http_client.aclose()
                self._http_client = None
            if self._stack is not None:
                with contextlib.suppress(Exception):
                    await self._stack.aclose()
                self._stack = None
            return

        if self._stack is not None:
            with contextlib.suppress(Exception):
                await self._stack.aclose()
            self._stack = None

        if self._http_client is not None:
            with contextlib.suppress(Exception):
                await self._http_client.aclose()
            self._http_client = None

        self._session = None
        self._connected = False
        logger.debug("MCP disconnected")

    # ------------------------------------------------------------------ context manager
    async def __aenter__(self) -> MCPClient:
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    # ------------------------------------------------------------------ helpers
    @property
    def is_connected(self) -> bool:
        return self._connected

    def __repr__(self) -> str:
        cfg = self.config
        if cfg.get("command"):
            return f"MCPClient(stdio: {cfg.get('command')} {cfg.get('args', [])})"
        if cfg.get("url"):
            return f"MCPClient(http: {cfg.get('url')})"
        return "MCPClient(unconfigured)"
