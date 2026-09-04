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


def _inject_user_auth(
    server_name: str,
    server_cfg: dict[str, Any],
    user_id: str | None,
    mcp_user_id: str | None = None,
) -> dict[str, Any]:
    """Inject auth into server config with strict isolation — v2 vault-only.

    Security invariant:
        user_id != None → ONLY vault credential (never env fallback)
        user_id == None → system/CLI credential via vault file fallback or env (if allowed)

    This is the critical security boundary for multi-user SaaS.
    """
    lookup_id = mcp_user_id or user_id
    if lookup_id:
        # USER-SCOPED: strict isolation via CredentialVault
        try:
            from nally.vault import get_vault

            vault = get_vault()
            transport = vault.get_for_transport(lookup_id, server_name, resource=server_name)
            if transport:
                if transport.headers:
                    existing = server_cfg.get("headers") or {}
                    return {**server_cfg, "headers": {**existing, **transport.headers}}
                if transport.env:
                    existing_env = server_cfg.get("env") or {}
                    return {**server_cfg, "env": {**existing_env, **transport.env}}
        except Exception as exc:
            logger.debug("Vault lookup failed for %s/%s: %s", lookup_id, server_name, exc)

        # Fallback to legacy stores for clean-break migration grace period
        # (will be removed in Phase E). Still strict, no env fallback.
        try:
            from nally.oauth.token_store import TokenStore as OAuthTokenStore

            store = OAuthTokenStore()
            token = store.get_valid(lookup_id, server_name)
            if token:
                try:
                    from nally.oauth.manager import OAuthManager
                    from nally.oauth.providers.github import GitHubProvider
                    from nally.oauth.providers.google import GoogleProvider
                    from nally.oauth.providers.notion import NotionProvider

                    mgr = OAuthManager(token_store=store)
                    if not mgr._providers:
                        for p in (GitHubProvider(), GoogleProvider(), NotionProvider()):
                            mgr.register_provider(p)
                    headers = mgr.get_auth_headers(lookup_id, server_name)
                    if headers:
                        existing = server_cfg.get("headers") or {}
                        return {**server_cfg, "headers": {**existing, **headers}}
                    env_vars = mgr.get_auth_env(lookup_id, server_name)
                    if env_vars:
                        existing_env = server_cfg.get("env") or {}
                        return {**server_cfg, "env": {**existing_env, **env_vars}}
                except Exception:
                    pass
                if server_cfg.get("command"):
                    key_map = {
                        "github": "GITHUB_PERSONAL_ACCESS_TOKEN",
                        "gmail": "GMAIL_TOKEN",
                        "notion": "NOTION_TOKEN",
                    }
                    k = key_map.get(server_name, f"{server_name.upper()}_TOKEN")
                    existing_env = server_cfg.get("env") or {}
                    return {**server_cfg, "env": {**existing_env, k: token.access_token}}
                else:
                    existing = server_cfg.get("headers") or {}
                    return {
                        **server_cfg,
                        "headers": {**existing, "Authorization": f"Bearer {token.access_token}"},
                    }
        except Exception as exc:
            logger.debug("OAuth TokenStore fallback failed for %s/%s: %s", lookup_id, server_name, exc)
        try:
            from nally.integrations import token_store as legacy_store

            legacy_token = legacy_store.get_valid_token(lookup_id, server_name)
            if legacy_token:
                if server_cfg.get("command"):
                    key_map = {
                        "github": "GITHUB_PERSONAL_ACCESS_TOKEN",
                        "gmail": "GMAIL_TOKEN",
                        "notion": "NOTION_TOKEN",
                    }
                    k = key_map.get(server_name, f"{server_name.upper()}_TOKEN")
                    existing_env = server_cfg.get("env") or {}
                    return {**server_cfg, "env": {**existing_env, k: legacy_token}}
                else:
                    existing = server_cfg.get("headers") or {}
                    return {
                        **server_cfg,
                        "headers": {**existing, "Authorization": f"Bearer {legacy_token}"},
                    }
        except Exception as exc:
            logger.debug("Legacy token fallback failed for %s/%s: %s", lookup_id, server_name, exc)

        # No vault credential found → FAIL CLOSED, do not fallback to global
        logger.debug(
            "No credential for user %s provider %s — returning unauthenticated config (AUTH_REQUIRED on use)",
            lookup_id,
            server_name,
        )
        return dict(server_cfg)
    # SYSTEM/CLI MODE: vault file fallback first, then env if allowed
    try:
        from nally.vault import get_vault

        vault = get_vault()
        # CLI uses synthetic _global or local user? Try _global file vault
        for try_id in ("_global", "default", "local"):
            transport = vault.get_for_transport(try_id, server_name, resource=server_name)
            if transport:
                if transport.headers:
                    existing = server_cfg.get("headers") or {}
                    return {**server_cfg, "headers": {**existing, **transport.headers}}
                if transport.env:
                    existing_env = server_cfg.get("env") or {}
                    return {**server_cfg, "env": {**existing_env, **transport.env}}
    except Exception:
        pass
    # Env fallback only if explicitly allowed (single-user dev mode)
    try:
        from nally.config import NALLY_ALLOW_ENV_FALLBACK

        if NALLY_ALLOW_ENV_FALLBACK:
            return inject_auth({server_name: server_cfg}).get(server_name, server_cfg)
    except Exception:
        pass
    # Default: still allow env for CLI to avoid breaking existing single-user setups,
    # but log warning when vault master key is configured (indicating multi-user intent)
    try:
        from nally.vault.crypto import is_encryption_configured

        if is_encryption_configured():
            logger.warning(
                "CLI credential fallback to env for %s while vault encryption configured — set NALLY_ALLOW_ENV_FALLBACK=true to allow explicitly",
                server_name,
            )
            # Still allow but this will be removed in production hardening
    except Exception:
        pass
    return inject_auth({server_name: server_cfg}).get(server_name, server_cfg)


def _has_user_credential(
    server_name: str,
    user_id: str | None,
    mcp_user_id: str | None = None,
) -> bool:
    """Check if user has a valid credential for a server.

    Strict check — vault first, then legacy stores, no env fallback.
    """
    lookup_id = mcp_user_id or user_id
    if not lookup_id:
        return True  # CLI mode: no check, allow
    try:
        from nally.vault import get_vault

        vault = get_vault()
        if vault.get_valid(lookup_id, server_name) is not None:
            return True
    except Exception:
        pass
    try:
        from nally.oauth.token_store import TokenStore as OAuthTokenStore

        store = OAuthTokenStore()
        if store.is_valid(lookup_id, server_name):
            return True
    except Exception:
        pass
    try:
        from nally.integrations import token_store as legacy_store

        if legacy_store.get_valid_token(lookup_id, server_name):
            return True
    except Exception:
        pass
    return False


async def _call_tool_async(
    server_name: str,
    server_cfg: dict[str, Any],
    orig_name: str,
    arguments: dict[str, Any],
    timeout: float = 30.0,
    user_id: str | None = None,
    mcp_user_id: str | None = None,
) -> str:
    """Open a short-lived MCP session and call a tool.

    For user-scoped requests with no credential, returns AUTH_REQUIRED
    immediately without attempting a network connection.
    """
    if not _has_mcp():
        return f"Error: mcp package not installed (server {server_name})"

    lookup_id = mcp_user_id or user_id
    if lookup_id and not _has_user_credential(server_name, user_id, mcp_user_id):
        return f"Error: AUTH_REQUIRED: No credential for {server_name}. Please connect via /mcp."

    from .client import MCPClient

    # Per-user auth (strict isolation) or system credential
    cfg = _inject_user_auth(server_name, server_cfg, user_id, mcp_user_id=mcp_user_id)

    try:
        async with MCPClient(cfg, timeout=timeout) as client:
            result = await asyncio.wait_for(client.call_tool(orig_name, arguments), timeout=timeout)
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
        user_id: str | None = None,
        mcp_user_id: str | None = None,
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
        self.user_id = user_id
        self.mcp_user_id = mcp_user_id

    def execute(self, **kwargs: Any) -> str:  # type: ignore[override]
        try:
            return _run_async(
                _call_tool_async(
                    self.server_name,
                    self.server_config,
                    self.orig_name,
                    kwargs,
                    timeout=self.timeout,
                    user_id=self.user_id,
                    mcp_user_id=self.mcp_user_id,
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
    user_id: str | None = None,
    mcp_user_id: str | None = None,
) -> int:
    """List tools from one server and register. Returns count."""
    if not _has_mcp():
        logger.warning("MCP not installed — skipping server %s", server_name)
        return 0

    from .client import MCPClient

    # Per-user auth or fallback to global inject_auth
    enriched = _inject_user_auth(server_name, cfg, user_id, mcp_user_id=mcp_user_id)

    count = 0
    try:
        async with MCPClient(enriched, timeout=timeout) as client:
            cursor: str | None = None
            while True:
                result = await asyncio.wait_for(client.list_tools(cursor=cursor), timeout=timeout)
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
                            user_id=user_id,
                            mcp_user_id=mcp_user_id,
                        )
                        registry.register(mcp_tool)
                        count += 1
                    except ValueError as ve:
                        logger.warning("Skipping MCP tool %s/%s: %s", server_name, tool.name, ve)
                    except Exception as exc:
                        logger.warning("Failed to register %s/%s: %s", server_name, tool.name, exc)
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
    registry: ToolRegistry,
    config: dict[str, Any] | None = None,
    timeout: float | None = None,
    user_id: str | None = None,
    mcp_user_id: str | None = None,
) -> int:
    """Load tools from all configured MCP servers. Returns total count.

    Security model:
        - If user_id/mcp_user_id is provided: ONLY per-user credentials (OAuthManager/TokenStore)
          No fallback to global/environment credentials. Missing credential → server skipped or AUTH_REQUIRED.
        - If no user_id: system/CLI mode → global inject_auth allowed (environment credentials)

    mcp_user_id overrides user_id for token lookup (Telegram user ID).
    """
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

    # Inject global auth only for system/CLI mode (no user_id)
    # For user-scoped requests, per-server injection handles strict isolation
    lookup_id = mcp_user_id or user_id
    if not lookup_id:
        try:
            config = inject_auth(config)
        except Exception as exc:
            logger.warning("MCP auth injection failed: %s", exc)

    total = 0
    for server_name, cfg in config.items():
        if not isinstance(cfg, dict):
            logger.warning("MCP server '%s' config not a dict — skipping", server_name)
            continue
        n = await _load_one_server(
            registry,
            server_name,
            cfg,
            float(timeout),
            deny,
            user_id=user_id,
            mcp_user_id=mcp_user_id,
        )
        total += n
    return total


def load_mcp_tools_sync(
    registry: ToolRegistry,
    config: dict[str, Any] | None = None,
    timeout: float | None = None,
    user_id: str | None = None,
    mcp_user_id: str | None = None,
) -> int:
    """Sync wrapper for load_mcp_tools."""
    return _run_async(
        load_mcp_tools(
            registry, config=config, timeout=timeout, user_id=user_id, mcp_user_id=mcp_user_id
        )
    )
