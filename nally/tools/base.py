"""Tool base class and registry — minimal, explicit, stdlib only.

Parameters use a custom mini-schema, NOT full JSON Schema::

    {
        "path": {
            "type": "string",       # one of: string, integer, number, boolean, array, object
            "description": "...",
            "required": True,
        }
    }

This is intentional for a tiny project.  Do not add a JSON Schema engine.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ToolStatus — machine-readable execution status
# ---------------------------------------------------------------------------


class ToolStatus(StrEnum):
    """Machine-readable tool execution status.

    AUTH_REQUIRED is typed, not string-parsed, so Telegram/UI can
    switch on it to show a Connect button instead of parsing error strings.
    """

    OK = "ok"
    AUTH_REQUIRED = "auth_required"
    ERROR = "error"


# ---------------------------------------------------------------------------
# ToolResult — structured return from tool execution
# ---------------------------------------------------------------------------
@dataclass
class ToolResult:
    """Structured result from a tool execution.

    Replaces the old (text, bool) tuple where success was detected by
    string-prefix parsing.

    New fields:
        status: Machine-readable status (OK, AUTH_REQUIRED, ERROR)
        structured_content: Optional structured data (preserved from MCP)
        output: Text content (for backward compat, same as content)
        success: Derived from status (for backward compat)
    """

    output: str
    success: bool = True
    tool_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    status: ToolStatus = ToolStatus.OK
    structured_content: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        # Keep success/status consistent
        if self.status == ToolStatus.OK and not self.success:
            # If caller set success=False but status still OK, fix status
            self.status = ToolStatus.ERROR
        elif self.status != ToolStatus.OK and self.success:
            # If status is error/auth but success True, fix success
            self.success = False

    @classmethod
    def auth_required(
        cls,
        provider: str,
        tool_name: str = "",
        message: str | None = None,
    ) -> ToolResult:
        """Create an AUTH_REQUIRED result for a provider."""
        msg = message or f"Authentication required for {provider}. Please connect via /mcp."
        return cls(
            output=f"Error: AUTH_REQUIRED: {msg}",
            success=False,
            tool_name=tool_name,
            status=ToolStatus.AUTH_REQUIRED,
            metadata={"provider": provider},
        )

    @classmethod
    def error(cls, message: str, tool_name: str = "") -> ToolResult:
        return cls(
            output=message if message.startswith("Error:") else f"Error: {message}",
            success=False,
            tool_name=tool_name,
            status=ToolStatus.ERROR,
        )

    @classmethod
    def ok(
        cls,
        output: str,
        tool_name: str = "",
        structured_content: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ToolResult:
        return cls(
            output=output,
            success=True,
            tool_name=tool_name,
            metadata=metadata or {},
            status=ToolStatus.OK,
            structured_content=structured_content,
        )


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------
class Tool:
    """Base class for all tools.

    Subclass and override `execute`. Declare `name`, `description`, and
    `parameters` (see module docstring for the mini-schema format).
    """

    name: str
    description: str
    parameters: dict[str, dict[str, Any]]

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        if not name or not name.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"Invalid tool name: {name!r}")
        self.name = name
        self.description = description.strip()
        self.parameters = parameters or {}

    # --- validation --------------------------------------------------------
    def validate(self, arguments: dict[str, Any]) -> tuple[bool, str]:
        """Validate arguments against self.parameters.

        Returns (ok, error_message). Empty message means valid.
        Simple, stdlib-only checker — not a full JSON Schema engine.
        """
        if not isinstance(arguments, dict):
            return False, "arguments must be an object"

        # Check required fields
        for param_name, spec in self.parameters.items():
            if spec.get("required") and param_name not in arguments:
                return False, f"missing required parameter: '{param_name}'"

        # Check unknown + type
        for key, value in arguments.items():
            spec = self.parameters.get(key)
            if spec is None:
                return False, f"unknown parameter: '{key}'"
            expected_type = spec.get("type")
            if expected_type and not _check_type(value, expected_type):
                return False, (
                    f"parameter '{key}' expected type '{expected_type}', got '{type(value).__name__}'"
                )
            # Enum check
            if "enum" in spec and value not in spec["enum"]:
                return False, f"parameter '{key}' must be one of {spec['enum']!r}"

        return True, ""

    # --- execution ---------------------------------------------------------
    def execute(self, **kwargs: Any) -> str | ToolResult:
        """Override in subclasses. Must return a string or ToolResult.

        New typed returns:
            return ToolResult.auth_required(provider="github")
            return ToolResult.ok(output="...")
        Legacy string returns remain supported.
        """
        raise NotImplementedError

    # --- OpenAI schema -----------------------------------------------------
    def to_openai_schema(self) -> dict[str, Any]:
        """Convert to OpenAI function-calling schema."""
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param_name, spec in self.parameters.items():
            prop: dict[str, Any] = {}
            if "type" in spec:
                prop["type"] = spec["type"]
            if "description" in spec:
                prop["description"] = spec["description"]
            if "enum" in spec:
                prop["enum"] = spec["enum"]
            properties[param_name] = prop
            if spec.get("required"):
                required.append(param_name)

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }


_VALID_TYPES = frozenset({"string", "integer", "number", "boolean", "array", "object"})


def _check_type(value: Any, expected: str) -> bool:
    """Check value matches expected type string.

    Raises ValueError for unknown types so that schema typos are caught
    at construction time, not silently accepted at runtime.
    """
    if expected not in _VALID_TYPES:
        raise ValueError(
            f"Unknown parameter type '{expected}'. Valid types: {sorted(_VALID_TYPES)}"
        )
    mapping = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    py_type = mapping[expected]
    # bool is subclass of int in Python, handle strictly
    if expected == "integer" and isinstance(value, bool):
        return False
    if expected == "number" and isinstance(value, bool):
        return False
    return isinstance(value, py_type)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
class ToolRegistry:
    """Holds tools, validates, executes, truncates."""

    def __init__(self, max_output: int = 8000) -> None:
        self._tools: dict[str, Tool] = {}
        self.max_output = max_output

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all_schemas(self) -> list[dict[str, Any]]:
        return [t.to_openai_schema() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def execute(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Validate and execute a tool.

        Returns (result_text, success). For typed results, use execute_result().
        """
        result, tool_result = self.execute_result(name, arguments)
        return result, tool_result.success if tool_result else not result.lstrip().startswith(
            "Error:"
        )

    def execute_result(self, name: str, arguments: dict[str, Any]) -> tuple[str, ToolResult]:
        """Validate and execute a tool, returning typed ToolResult.

        Returns (result_text, ToolResult). The ToolResult contains
        machine-readable status (OK, AUTH_REQUIRED, ERROR).
        """
        tool = self._tools.get(name)
        # Tolerant lookup: models often mangle MCP names (drop underscores, drop mcp_ prefix)
        if tool is None:
            def _norm(s: str) -> str:
                return s.replace("_", "").replace("-", "").lower()

            target = _norm(name)
            matches = [n for n in self._tools if _norm(n) == target]
            # Also: short name without mcp_ prefix → mcp_{server}_{name}
            if not matches and not name.startswith("mcp_"):
                matches = [n for n in self._tools if n.endswith(f"_{name}") or n.endswith(f"__{name}")]
            if len(matches) == 1:
                name = matches[0]
                tool = self._tools.get(name)
            elif len(matches) > 1:
                # Prefer github if ambiguous
                preferred = [m for m in matches if "github" in m] or matches
                name = preferred[0]
                tool = self._tools.get(name)
        if tool is None:
            available = sorted(self._tools.keys())
            mcp_tools = [n for n in available if n.startswith("mcp_")]
            hint = ""
            if mcp_tools:
                sample = ", ".join(mcp_tools[:12])
                more = f" (+{len(mcp_tools)-12} more)" if len(mcp_tools) > 12 else ""
                hint = f" Available MCP tools: {sample}{more}. Use these exact names."
            elif any(n.startswith("mcp") for n in available):
                hint = " MCP tools present under different naming."
            else:
                hint = (
                    " No MCP tools are loaded. User must connect the provider "
                    "(/mcp → Connect GitHub) and NALLY_MCP_ENABLED must be true."
                )
            msg = f"Error: unknown tool '{name}'.{hint}"
            return msg, ToolResult.error(msg, tool_name=name)

        # Validate before execution
        ok, err = tool.validate(arguments)
        if not ok:
            msg = f"Error: invalid arguments for '{name}': {err}"
            return msg, ToolResult.error(msg, tool_name=name)

        try:
            raw = tool.execute(**arguments)
            # Handle typed ToolResult return
            if isinstance(raw, ToolResult):
                output = raw.output
                # Truncate if needed
                if len(output) > self.max_output:
                    output = (
                        output[: self.max_output] + f"\n... [truncated, {len(output)} chars total]"
                    )
                    raw.output = output
                return output, raw
            # Legacy string return
            result = raw
            if not isinstance(result, str):
                try:
                    result = json.dumps(result, ensure_ascii=False, indent=2)
                except Exception:
                    result = str(result)
        except Exception as exc:
            logger.exception("Tool '%s' raised %s: %s", name, type(exc).__name__, exc)
            msg = f"Error executing '{name}': {type(exc).__name__}: {exc}"
            return msg, ToolResult.error(msg, tool_name=name)

        # Truncate
        if len(result) > self.max_output:
            result = result[: self.max_output] + f"\n... [truncated, {len(result)} chars total]"

        # Determine typed status from string prefix (legacy compat)
        # "Error: AUTH_REQUIRED" → ToolStatus.AUTH_REQUIRED
        stripped = result.lstrip()
        if stripped.startswith("Error: AUTH_REQUIRED") or "AUTH_REQUIRED" in stripped[:100]:
            # Try to extract provider from metadata if possible
            provider = "unknown"
            # Heuristic: look for provider name in message
            for p in ("github", "gmail", "notion"):
                if p in stripped.lower():
                    provider = p
                    break
            tr = ToolResult.auth_required(provider=provider, tool_name=name, message=result)
            # Preserve original output
            tr.output = result
            return result, tr
        elif stripped.startswith("Error:"):
            return result, ToolResult.error(result, tool_name=name)
        else:
            return result, ToolResult.ok(result, tool_name=name)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
