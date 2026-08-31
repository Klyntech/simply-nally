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
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ToolResult — structured return from tool execution
# ---------------------------------------------------------------------------
@dataclass
class ToolResult:
    """Structured result from a tool execution.

    Replaces the old (text, bool) tuple where success was detected by
    string-prefix parsing.
    """

    output: str
    success: bool
    tool_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


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
    def execute(self, **kwargs: Any) -> str:
        """Override in subclasses. Must return a string."""
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
            f"Unknown parameter type '{expected}'. "
            f"Valid types: {sorted(_VALID_TYPES)}"
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

        Returns (result_text, success).
        """
        tool = self._tools.get(name)
        # Alias: allow short name without mcp prefix (model sometimes drops it)
        if tool is None and "__" not in name:
            candidates = [n for n in self._tools if n.endswith(f"__{name}")]
            if len(candidates) == 1:
                tool = self._tools.get(candidates[0])
                name = candidates[0]
            elif len(candidates) > 1:
                github = [c for c in candidates if "github" in c]
                if github:
                    tool = self._tools.get(github[0])
                    name = github[0]
        if tool is None:
            return f"Error: unknown tool '{name}'", False

        # Validate before execution
        ok, err = tool.validate(arguments)
        if not ok:
            return f"Error: invalid arguments for '{name}': {err}", False

        try:
            result = tool.execute(**arguments)
            # Ensure string
            if not isinstance(result, str):
                try:
                    result = json.dumps(result, ensure_ascii=False, indent=2)
                except Exception:
                    result = str(result)
        except Exception as exc:
            logger.exception("Tool '%s' raised %s: %s", name, type(exc).__name__, exc)
            return f"Error executing '{name}': {type(exc).__name__}: {exc}", False

        # Truncate
        if len(result) > self.max_output:
            result = result[: self.max_output] + f"\n... [truncated, {len(result)} chars total]"

        # Structured success — no more string-prefix parsing.
        # Errors from tools always start with "Error:" by convention.
        success = not result.lstrip().startswith("Error:")
        return result, success

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
