"""Simply NALLY config - single source of truth, no side effects.

Loads from environment (.env supported). No directory creation or logging at import.
"""

from __future__ import annotations

import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Provider / LLM
# ---------------------------------------------------------------------------
PROVIDER: str = os.getenv("NALLY_PROVIDER", "openai").strip().lower()


# API key: provider-specific first, then generic fallback
# Opencode supports comma-separated keys (rotation) - we use the first for now
def _first_key(env_name: str) -> str:
    raw = os.getenv(env_name, "").strip()
    if "," in raw:
        # Take first non-empty key (mirrors old N.A.L.L.Y rotation logic)
        for part in raw.split(","):
            part = part.strip()
            if part:
                return part
        return ""
    return raw


_API_KEY_MAP = {
    "openai": _first_key("OPENAI_API_KEY"),
    "groq": _first_key("GROQ_API_KEY"),
    "opencode": _first_key("OPENCODE_API_KEY"),
    "nvidia": _first_key("NVIDIA_API_KEY"),
}

# Allow OPENAI_API_KEY as generic fallback for any provider
GENERIC_API_KEY = _first_key("OPENAI_API_KEY") or _first_key("API_KEY")

API_KEY: str = _API_KEY_MAP.get(PROVIDER, "") or GENERIC_API_KEY

# Base URL: explicit override wins, else provider defaults
_EXPLICIT_BASE_URL = os.getenv("OPENAI_BASE_URL", "") or os.getenv("BASE_URL", "")

_PROVIDER_BASE_URL = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "opencode": "https://opencode.ai/zen/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
}

BASE_URL: str = _EXPLICIT_BASE_URL or _PROVIDER_BASE_URL.get(PROVIDER, "https://api.openai.com/v1")

# Model: explicit wins, else provider default
_EXPLICIT_MODEL = os.getenv("NALLY_MODEL", "") or os.getenv("MODEL", "")

_PROVIDER_DEFAULT_MODEL = {
    "openai": "gpt-4o-mini",
    "groq": "llama-3.3-70b-versatile",
    # hy3-free retired 2026-08 — use ling-3.0-flash-fin-free or mimo-v2.5-free (free tier, flaky)
    # For stable free: use Vmcj key + ling-3.0-flash-fin-free (needs max_tokens >= 800)
    "opencode": "ling-3.0-flash-fin-free",
    # Nvidia NIM — super is 3x faster than 3.5-lightning (3.3s vs 22-39s for tool calls, tested 2026-08-31)
    # super 120b: 3.0s (thinking False) / 7.5s (default) for limerick; 3.3s for tool calls
    "nvidia": "nvidia/nemotron-3-super-120b-a12b",
}

MODEL: str = _EXPLICIT_MODEL or _PROVIDER_DEFAULT_MODEL.get(PROVIDER, "gpt-4o-mini")


# ---------------------------------------------------------------------------
# Agent limits
# ---------------------------------------------------------------------------
def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (ValueError, AttributeError):
        return default


MAX_ITERATIONS: int = _int_env("NALLY_MAX_ITERATIONS", 10)
MAX_TOOL_CALLS: int = _int_env("NALLY_MAX_TOOL_CALLS", 20)
MAX_TOOL_OUTPUT: int = _int_env("NALLY_MAX_TOOL_OUTPUT", 8000)  # chars per tool
MAX_TOOL_OUTPUT_TOTAL: int = _int_env("NALLY_MAX_TOOL_OUTPUT_TOTAL", 30000)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
DEFAULT_SYSTEM_PROMPT = (
    "You are Nally, a direct, capable assistant. You handle code, planning, "
    "writing, research, and general problem-solving with equal competence.\n"
    "\n"
    "# Your Tools\n"
    "You have six tools. Use them when they help. Never use a tool when your "
    "existing knowledge is sufficient.\n"
    "read_file - Read a file's contents. Use when you need to see code, config, "
    "or content before answering. Never guess at file contents - always read first.\n"
    "write_file - Create or overwrite a file within the workspace. Use when the "
    "user asks you to create, generate, or save something.\n"
    "list_dir - List files and folders in a directory. Use when you need to "
    "understand project structure or find files.\n"
    "run_command - Execute a shell command. Use when the user asks you to run "
    "something, install something, or you need to verify code works. Never run "
    "destructive operations (rm -rf, drop database) without asking first.\n"
    "web_search - Search the internet for current information. Use when the user "
    "asks about something you are unsure about, current events, or recent releases. "
    "Do not waste a search on common knowledge.\n"
    "fetch - Read the contents of a URL. Use when you need to read a specific "
    "webpage, API docs, or article.\n"
    "\n"
    "# Domains\n"
    "Code - Read before writing. Explain your approach before implementing. "
    "Always use fenced code blocks with the language tag. If modifying existing "
    "code, read the file first to understand context.\n"
    "Planning - Break complex tasks into numbered steps. Identify dependencies "
    "between steps. Ask before executing destructive actions.\n"
    "Writing - Match tone to purpose. Edit for clarity. Remove filler. Be direct.\n"
    "Research - Search first, then synthesize. Cite sources when relevant. "
    "Distinguish facts from opinions.\n"
    "General - Be concise. Default to short answers unless detail is requested. "
    "For simple questions, answer directly without tools. For complex or "
    "uncertain tasks, use your tools systematically.\n"
    "\n"
    "# Guardrails\n"
    "- Never fabricate file contents. Always read_file first.\n"
    "- Never guess at code behavior. Read the code.\n"
    "- If a tool returns an error, try a different approach before giving up.\n"
    "- If you do not know something, say so. Do not make up answers.\n"
    "- Keep responses concise. Expand only when the user asks for detail or the "
    "topic demands it.\n"
    "- When writing code, include all necessary imports and context so it runs "
    "standalone.\n"
    "- Never expose secrets, API keys, or credentials in output.\n"
    "\n"
    "# Output Format\n"
    '- Default: Direct answer, no preamble. No "Great question!" or "I\'d be '
    'happy to help."\n'
    "- Code: Fenced blocks with language tag. Include imports. Keep it runnable.\n"
    "- Plans: Numbered steps with brief explanations.\n"
    "- Research: Key findings first, then details. Cite sources inline.\n"
    "- Errors: State what went wrong and what you tried. Suggest next steps."
)


def get_system_prompt() -> str:
    """Return system prompt, allowing override via env."""
    return os.getenv("NALLY_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)


# ---------------------------------------------------------------------------
# v2 Auth — browser-first, vault, session isolation
# ---------------------------------------------------------------------------
NALLY_AUTH_V2: bool = os.getenv("NALLY_AUTH_V2", "true").strip().lower() in ("1", "true", "yes", "on")
NALLY_VAULT_MASTER_KEY: str = os.getenv("NALLY_VAULT_MASTER_KEY", "").strip()
OAUTH_BASE_URL: str = (os.getenv("OAUTH_BASE_URL", "").strip() or os.getenv("WEBHOOK_BASE_URL", "").strip())
# Single-user dev mode: explicit env token fallback (disabled by default in multi-user)
NALLY_ALLOW_ENV_FALLBACK: bool = os.getenv("NALLY_ALLOW_ENV_FALLBACK", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GOOGLE_DEVICE_CLIENT_ID: str = os.getenv("GOOGLE_DEVICE_CLIENT_ID", "").strip()
GOOGLE_DEVICE_CLIENT_SECRET: str = os.getenv("GOOGLE_DEVICE_CLIENT_SECRET", "").strip()

# ---------------------------------------------------------------------------
# MCP — Model Context Protocol (opt-in, v1: GitHub + Gmail + Notion only)
# ---------------------------------------------------------------------------
MCP_ENABLED: bool = os.getenv("NALLY_MCP_ENABLED", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
MCP_TIMEOUT: int = _int_env("NALLY_MCP_TIMEOUT", 30)
MCP_DENY: list[str] = [s.strip() for s in os.getenv("NALLY_MCP_DENY", "").split(",") if s.strip()]
# Supported MCP servers — v1 lock
SUPPORTED_MCP_SERVERS: set[str] = {"github", "gmail", "notion"}
# JSON map of server_name -> config (command/url/etc). Prefer explicit per-server env.
MCP_SERVERS_JSON: str = os.getenv("NALLY_MCP_SERVERS", "").strip()

# GitHub MCP — OAuth for all (remote Streamable HTTP default)
GITHUB_MCP_URL: str = os.getenv("GITHUB_MCP_URL", "https://api.githubcopilot.com/mcp/").strip()
GITHUB_MCP_PAT: str = (
    os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "").strip() or os.getenv("GITHUB_TOKEN", "").strip()
)
GITHUB_MCP_TOOLSETS: str = os.getenv("GITHUB_TOOLSETS", "").strip()
GITHUB_MCP_READ_ONLY: bool = os.getenv("GITHUB_READ_ONLY", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# Local stdio fallback (Docker or binary). If set, stdio takes precedence over URL.
GITHUB_MCP_COMMAND: str = os.getenv("GITHUB_MCP_COMMAND", "").strip()
GITHUB_MCP_ARGS: str = os.getenv("GITHUB_MCP_ARGS", "").strip()
# GitHub OAuth app credentials (for OAuth flow)
GITHUB_CLIENT_ID: str = os.getenv("GITHUB_CLIENT_ID", "").strip()
GITHUB_CLIENT_SECRET: str = os.getenv("GITHUB_CLIENT_SECRET", "").strip()
GITHUB_OAUTH_SCOPES: str = os.getenv("GITHUB_OAUTH_SCOPES", "").strip()
# Notion MCP — NOTION_TOKEN for local stdio, NOTION_MCP_URL for remote
NOTION_TOKEN: str = os.getenv("NOTION_TOKEN", "").strip()
NOTION_MCP_URL: str = os.getenv("NOTION_MCP_URL", "https://mcp.notion.com/mcp").strip()
NOTION_MCP_COMMAND: str = os.getenv("NOTION_MCP_COMMAND", "").strip()
NOTION_MCP_ARGS: str = os.getenv("NOTION_MCP_ARGS", "").strip()
# Notion OAuth (PKCE flow for remote MCP — dynamic client registration)
NOTION_CLIENT_ID: str = os.getenv("NOTION_CLIENT_ID", "").strip()
NOTION_CLIENT_SECRET: str = os.getenv("NOTION_CLIENT_SECRET", "").strip()
NOTION_OAUTH_SCOPES: str = os.getenv("NOTION_OAUTH_SCOPES", "").strip()
NOTION_CALLBACK_PORT: int = int(os.getenv("NOTION_CALLBACK_PORT", "8080"))
# Gmail MCP — Google official remote (Streamable HTTP, no npm/npx needed)
# Docs: https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server
# Requires: Google Cloud project with Gmail API + gmailmcp.googleapis.com enabled,
# OAuth consent with scopes gmail.readonly + gmail.compose, and a Web/Desktop OAuth client.
GMAIL_MCP_URL: str = os.getenv("GMAIL_MCP_URL", "https://gmailmcp.googleapis.com/mcp").strip()
GMAIL_TOKEN: str = (
    os.getenv("GMAIL_TOKEN", "").strip()
    or os.getenv("GMAIL_OAUTH_TOKEN", "").strip()
    or os.getenv("GOOGLE_GMAIL_TOKEN", "").strip()
)
# Optional local stdio fallback (if you run a community gmail-mcp server locally)
GMAIL_MCP_COMMAND: str = os.getenv("GMAIL_MCP_COMMAND", "").strip()
GMAIL_MCP_ARGS: str = os.getenv("GMAIL_MCP_ARGS", "").strip()
# Gmail OAuth app credentials (for `python main.py mcp gmail-login` device flow)
GMAIL_CLIENT_ID: str = os.getenv("GMAIL_CLIENT_ID", "").strip()
GMAIL_CLIENT_SECRET: str = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
GMAIL_OAUTH_SCOPES: str = os.getenv(
    "GMAIL_OAUTH_SCOPES",
    "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.compose",
).strip()


def get_mcp_servers_config() -> dict:
    """Return dict server_name -> config for enabled MCP servers.

    Returns raw transport config only (command/url/args). Auth is injected
    separately by ``nally.mcp.auth.inject_auth`` at connection time so this
    module stays data-only and does not import ``github_oauth``.

    Priority:
      1. NALLY_MCP_SERVERS JSON (if valid, overrides per-server env)
      2. Per-server env (GitHub, Notion, Gmail)

    Returns {} when MCP not enabled. Rejects unsupported servers.
    """
    if not MCP_ENABLED:
        return {}
    # 1) JSON wins — caller-supplied headers/env are preserved verbatim
    if MCP_SERVERS_JSON:
        try:
            import json as _json

            data = _json.loads(MCP_SERVERS_JSON)
            if isinstance(data, dict) and data:
                # Filter unsupported servers
                filtered = {k: v for k, v in data.items() if k in SUPPORTED_MCP_SERVERS}
                rejected = set(data.keys()) - SUPPORTED_MCP_SERVERS
                if rejected:
                    import logging

                    logging.getLogger(__name__).warning(
                        "Ignoring unsupported MCP servers: %s", ", ".join(sorted(rejected))
                    )
                return filtered
        except Exception:
            pass
    # 2) Per-server env — transport only, no credential injection
    cfg: dict = {}
    gh: dict = {}
    if GITHUB_MCP_COMMAND:
        gh["command"] = GITHUB_MCP_COMMAND
        if GITHUB_MCP_ARGS:
            gh["args"] = GITHUB_MCP_ARGS.split()
    else:
        gh["url"] = GITHUB_MCP_URL
        if GITHUB_MCP_TOOLSETS:
            gh.setdefault("headers", {})["X-MCP-Toolsets"] = GITHUB_MCP_TOOLSETS
        if GITHUB_MCP_READ_ONLY:
            gh.setdefault("headers", {})["X-MCP-Readonly"] = "true"
    if gh:
        cfg["github"] = gh
    nt: dict = {}
    if NOTION_MCP_COMMAND:
        nt["command"] = NOTION_MCP_COMMAND
        if NOTION_MCP_ARGS:
            nt["args"] = NOTION_MCP_ARGS.split()
    else:
        nt["url"] = NOTION_MCP_URL
    if nt:
        cfg["notion"] = nt
    gm: dict = {}
    if GMAIL_MCP_COMMAND:
        gm["command"] = GMAIL_MCP_COMMAND
        if GMAIL_MCP_ARGS:
            gm["args"] = GMAIL_MCP_ARGS.split()
    else:
        # Google official remote — no npm/npx needed, pure HTTP
        gm["url"] = GMAIL_MCP_URL
    if gm:
        cfg["gmail"] = gm
    return cfg


def validate_config(require_api_key: bool = True) -> list[str]:
    """Return list of error strings; empty means valid."""
    errors: list[str] = []
    if require_api_key and not API_KEY:
        errors.append(
            f"Missing API key for provider '{PROVIDER}'. "
            f"Set {'OPENAI_API_KEY' if PROVIDER == 'openai' else PROVIDER.upper() + '_API_KEY'} in .env"
        )
    if MAX_ITERATIONS < 1:
        errors.append("NALLY_MAX_ITERATIONS must be >= 1")
    if MAX_TOOL_CALLS < 1:
        errors.append("NALLY_MAX_TOOL_CALLS must be >= 1")
    return errors
