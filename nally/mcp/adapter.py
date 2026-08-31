"""MCP adapter — bridges MCP servers to ToolRegistry.

Converts MCP-discovered tools into NALLY's normalized Tool abstraction.
Agent never knows whether a tool came from filesystem.py or an MCP server.

Depends on:
    • mcp/client.py — transport + session (MCPClient)
    • mcp/auth.py  — credential resolution (inject_auth)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from nally.tools.base import Tool, ToolRegistry

from .auth import inject_auth

logger = logging.getLogger(__name__)


def _mcp_schema_to_params(input_schema: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Convert MCP JSON Schema inputSchema -> ToolRegistry parameters."""
    if not isinstance(input_schema, dict):
        return {}
    properties = input_schema.get("properties")
    if not isinstance(properties, dict):
        if "anyOf" in input_schema or "oneOf" in input_schema:
            logger.warning("MCP inputSchema has anyOf/oneOf — degrading to string 'input'")
            return {
                "input": {
                    "type": "string",
                    "description": "JSON string input (schema had anyOf/oneOf)",
                    "required": False,
                }
            }
        return {}
    required = set(input_schema.get("required", []) or [])
    params: dict[str, dict[str, Any]] = {}
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            continue
        if any(k in spec for k in ("anyOf", "oneOf", "allOf")):
            params[name] = {
                "type": "string",
                "description": spec.get("description", f"JSON string for {name} (complex schema)"),
                "required": name in required,
            }
            continue
        t = spec.get("type", "string")
        if t not in ("string", "integer", "number", "boolean", "array", "object"):
            t = "string"
        entry: dict[str, Any] = {"type": t, "required": name in required}
        if "description" in spec:
            entry["description"] = spec["description"]
        if "enum" in spec:
            entry["enum"] = spec["enum"]
        params[name] = entry
    return params


def _flatten_mcp_result(content: list[Any], is_error: bool | None, structured: Any | None) -> str:
    """Flatten MCP CallToolResult content[] -> single string."""
    parts: list[str] = []
    for item in content or []:
        try:
            t = getattr(item, "type", None)
            if t == "text":
                txt = getattr(item, "text", "") or ""
                if txt:
                    parts.append(txt)
            elif t == "image":
                mime = getattr(item, "mimeType", "image")
                data = getattr(item, "data", "") or ""
                parts.append(f"[image: {mime}, {len(str(data))} chars base64]")
            elif t == "resource":
                res = getattr(item, "resource", None)
                if res is not None:
                    uri = getattr(res, "uri", "") or ""
                    txt2 = getattr(res, "text", "") or ""
                    if txt2:
                        parts.append(f"[resource {uri}]: {txt2}")
                    else:
                        parts.append(f"[resource {uri}]")
                else:
                    parts.append(str(item))
            else:
                txt3 = getattr(item, "text", None)
                if txt3:
                    parts.append(str(txt3))
                else:
                    parts.append(str(item))
        except Exception:
            parts.append(str(item))
    text = "\n\n".join(parts).strip()
    if not text and structured is not None:
        try:
            text = json.dumps(structured, ensure_ascii=False, indent=2)
        except Exception:
            text = str(structured)
    if not text:
        text = "(empty MCP result)"
    if is_error and not text.lstrip().lower().startswith("error"):
        text = f"Error: {text}"
    return text


def _run_async(coro):
    """Run coro synchronously, handling already-running loops."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures

        future: concurrent.futures.Future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=60)
    else:
        return asyncio.run(coro)


def _has_mcp() -> bool:
    try:
        import mcp  # noqa: F401

        return True
    except ImportError:
        return False


async def _call_tool_async(
    server_name: str,
    server_cfg: dict[str, Any],
    orig_name: str,
    arguments: dict[str, Any],
    timeout: float = 30.0,
) -> str:
    """Open a short-lived MCP session and call a tool."""
    if not _has_mcp():
        return f"Error: mcp package not installed (server {server_name})"

    from .client import MCPClient

    # Ensure auth headers are present (no-op if already there)
    cfg = inject_auth({server_name: server_cfg}).get(server_name, server_cfg)

    try:
        async with MCPClient(cfg, timeout=timeout) as client:
            result = await asyncio.wait_for(
                client.call_tool(orig_name, arguments), timeout=timeout
            )
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return f"Error: mcp {server_name}/{orig_name}: {type(exc).__name__}: {exc}"

    return _flatten_mcp_result(
        result.content, result.isError, getattr(result, "structuredContent", None)
    )


class MCPTool(Tool):
    """ToolRegistry wrapper around an MCP server tool."""

    def __init__(
        self,
        server_name: str,
        orig_name: str,
        description: str,
        input_schema: dict[str, Any] | None,
        server_config: dict[str, Any],
        timeout: float = 30.0,
    ) -> None:
        params = _mcp_schema_to_params(input_schema)
        namespaced = f"mcp__{server_name}__{orig_name}"
        super().__init__(
            name=namespaced,
            description=description or f"MCP tool {orig_name} from {server_name}",
            parameters=params,
        )
        self.server_name = server_name
        self.orig_name = orig_name
        self.server_config = server_config
        self.timeout = timeout

    def execute(self, **kwargs: Any) -> str:  # type: ignore[override]
        try:
            return _run_async(
                _call_tool_async(
                    self.server_name,
                    self.server_config,
                    self.orig_name,
                    kwargs,
                    timeout=self.timeout,
                )
            )
        except Exception as exc:
            return f"Error: mcp {self.server_name}/{self.orig_name}: {type(exc).__name__}: {exc}"


async def _load_one_server(
    registry: ToolRegistry,
    server_name: str,
    cfg: dict[str, Any],
    timeout: float,
    deny: set[str],
) -> int:
    """List tools from one server and register. Returns count."""
    if not _has_mcp():
        logger.warning("MCP not installed — skipping server %s", server_name)
        return 0

    from .client import MCPClient

    # Inject auth before connecting (preserves existing headers)
    enriched = inject_auth({server_name: cfg}).get(server_name, cfg)

    count = 0
    try:
        async with MCPClient(enriched, timeout=timeout) as client:
            cursor: str | None = None
            while True:
                result = await asyncio.wait_for(
                    client.list_tools(cursor=cursor), timeout=timeout
                )
                for tool in result.tools:
                    if tool.name in deny or f"mcp__{server_name}__{tool.name}" in deny:
                        continue
                    try:
                        mcp_tool = MCPTool(
                            server_name=server_name,
                            orig_name=tool.name,
                            description=tool.description or "",
                            input_schema=tool.inputSchema
                            if isinstance(tool.inputSchema, dict)
                            else None,
                            server_config=enriched,
                            timeout=timeout,
                        )
                        registry.register(mcp_tool)
                        count += 1
                    except ValueError as ve:
                        logger.warning(
                            "Skipping MCP tool %s/%s: %s", server_name, tool.name, ve
                        )
                    except Exception as exc:
                        logger.warning(
                            "Failed to register %s/%s: %s", server_name, tool.name, exc
                        )
                cursor = getattr(result, "nextCursor", None)
                if not cursor:
                    break
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        logger.warning(
            "MCP server '%s' failed to list tools: %s: %s", server_name, type(exc).__name__, exc
        )
        return 0

    logger.info("MCP server '%s' registered %d tools", server_name, count)
    return count


async def load_mcp_tools(
    registry: ToolRegistry, config: dict[str, Any] | None = None, timeout: float | None = None
) -> int:
    """Load tools from all configured MCP servers. Returns total count."""
    if config is None:
        try:
            from nally.config import MCP_TIMEOUT, get_mcp_servers_config

            config = get_mcp_servers_config()
            if timeout is None:
                timeout = float(MCP_TIMEOUT)
        except Exception:
            config = {}
            if timeout is None:
                timeout = 30.0
    if timeout is None:
        timeout = 30.0
    if not config:
        return 0
    try:
        from nally.config import MCP_DENY

        deny = set(MCP_DENY or [])
    except Exception:
        deny = set()

    # Inject auth once for all servers (avoids per-server injection overhead)
    try:
        config = inject_auth(config)
    except Exception as exc:
        logger.warning("MCP auth injection failed: %s", exc)

    total = 0
    for server_name, cfg in config.items():
        if not isinstance(cfg, dict):
            logger.warning("MCP server '%s' config not a dict — skipping", server_name)
            continue
        n = await _load_one_server(registry, server_name, cfg, float(timeout), deny)
        total += n
    return total


def load_mcp_tools_sync(
    registry: ToolRegistry, config: dict[str, Any] | None = None, timeout: float | None = None
) -> int:
    """Sync wrapper for load_mcp_tools."""
    return _run_async(load_mcp_tools(registry, config=config, timeout=timeout))
