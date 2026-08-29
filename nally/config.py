"""Simply NALLY config — single source of truth, no side effects.

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
# Opencode supports comma-separated keys (rotation) — we use the first for now
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
    "opencode": "muse-spark-1.2-contributor-free",
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
    "You are Nally, a helpful and concise assistant. "
    "Use tools when they help answer the user accurately. "
    "If you use a tool, explain briefly what you did afterward. "
    "Be direct and avoid unnecessary formatting."
)


def get_system_prompt() -> str:
    """Return system prompt, allowing override via env."""
    return os.getenv("NALLY_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)


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
