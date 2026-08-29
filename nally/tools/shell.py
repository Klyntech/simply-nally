"""Shell tool — run a command safely with timeout and output capture."""

from __future__ import annotations

import subprocess

from .base import Tool


class RunCommand(Tool):
    def __init__(self, default_timeout: int = 30) -> None:
        super().__init__(
            name="run_command",
            description=(
                "Execute a shell command and return its output. "
                "Use for running scripts, builds, or system queries. "
                "Output includes stdout, stderr, and exit code."
            ),
            parameters={
                "command": {
                    "type": "string",
                    "description": "Shell command to execute",
                    "required": True,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30, max 120)",
                    "required": False,
                },
            },
        )
        self.default_timeout = default_timeout

    def execute(self, command: str = "", timeout: int | None = None, **kwargs) -> str:  # type: ignore[override]
        if not command or not command.strip():
            return "Error: command must not be empty"
        if len(command) > 8000:
            return "Error: command too long (max 8000 chars)"
        if timeout is None:
            timeout = self.default_timeout
        # Clamp timeout
        try:
            timeout = int(timeout)
        except (ValueError, TypeError):
            return "Error: timeout must be an integer"
        if timeout < 1:
            timeout = 1
        if timeout > 120:
            timeout = 120

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            out = result.stdout or ""
            err = result.stderr or ""
            combined = ""
            if out:
                combined += out
                if not combined.endswith("\n"):
                    combined += "\n"
            if err:
                combined += f"[stderr]\n{err}"
                if not combined.endswith("\n"):
                    combined += "\n"
            if not combined:
                combined = "(no output)\n"
            combined += f"Exit code: {result.returncode}"
            return combined
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {timeout}s: {command[:200]}"
        except Exception as exc:
            return f"Error: failed to run command: {type(exc).__name__}: {exc}"


def register_shell_tools(registry) -> None:
    registry.register(RunCommand())
