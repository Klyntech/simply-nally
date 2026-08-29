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
}

BASE_URL: str = _EXPLICIT_BASE_URL or _PROVIDER_BASE_URL.get(PROVIDER, "https://api.openai.com/v1")

# Model: explicit wins, else provider default
_EXPLICIT_MODEL = os.getenv("NALLY_MODEL", "") or os.getenv("MODEL", "")

_PROVIDER_DEFAULT_MODEL = {
    "openai": "gpt-4o-mini",
    "groq": "llama-3.3-70b-versatile",
    "opencode": "hy3-free",
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
    "You have seven tools. Use them when they help. Never use a tool when your "
    "existing knowledge is sufficient.\n"
    "think - Internal reasoning. Use before acting on complex tasks to plan your "
    "approach. Hidden from the user.\n"
    "read_file - Read a file's contents. Use when you need to see code, config, "
    "or content before answering. Never guess at file contents - always read first.\n"
    "write_file - Create or overwrite a file. Use when the user asks you to "
    "create, generate, or save something.\n"
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
    "- Default: Direct answer, no preamble. No \"Great question!\" or \"I'd be "
    "happy to help.\"\n"
    "- Code: Fenced blocks with language tag. Include imports. Keep it runnable.\n"
    "- Plans: Numbered steps with brief explanations.\n"
    "- Research: Key findings first, then details. Cite sources inline.\n"
    "- Errors: State what went wrong and what you tried. Suggest next steps."
)


def get_system_prompt() -> str:
    """Return system prompt, allowing override via env."""
    return os.getenv("NALLY_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GOOGLE_DEVICE_CLIENT_ID: str = os.getenv("GOOGLE_DEVICE_CLIENT_ID", "").strip()
GOOGLE_DEVICE_CLIENT_SECRET: str = os.getenv("GOOGLE_DEVICE_CLIENT_SECRET", "").strip()


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
