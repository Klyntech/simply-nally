"""Shell tool — run a command with timeout, output capture, and policy check.

This is a **privileged** tool.  ``shell=True`` means the LLM can execute
arbitrary commands on the host OS.  The system prompt saying "don't do
dangerous things" is not a permission system.

``ShellPolicy`` provides a hook for future allowlist/blocklist enforcement.
For v0.x it logs every invocation and passes through.  A real policy
implementation would check commands against an allowlist before execution.
"""

from __future__ import annotations

import logging
import re
import subprocess

from .base import Tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Policy — pluggable command filter
# ---------------------------------------------------------------------------

# Default blocklist: destructive commands that should always be rejected.
_DEFAULT_BLOCKED = [
    re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*\s+.*/"),  # rm -rf / or rm -rfr / etc
    re.compile(r"\bmkfs\b"),           # format filesystem
    re.compile(r"\bdd\s+.*of=/dev/"),  # dd to device
]

# Prefixes that indicate potentially destructive operations.
# Logged as warnings but not blocked in v0.x.
_DESTRUCTIVE_PREFIXES = ("rm ", "rmdir ", "mkfs", "dd ", "format ", "shutdown", "reboot")


class ShellPolicy:
    """Command policy hook.

    Subclass and override ``check`` to implement real allowlist/blocklist.
    In v0.x, ``check`` always returns ``True`` but logs the invocation.
    """

    def __init__(self) -> None:
        self._blocked: list[re.Pattern[str]] = list(_DEFAULT_BLOCKED)

    def check(self, command: str) -> tuple[bool, str]:
        """Return (allowed, reason).  If not allowed, command is rejected."""
        for pat in self._blocked:
            if pat.search(command):
                return False, f"command matches blocked pattern: {pat.pattern}"
        return True, ""


# Module-level default policy (replaced in tests)
_default_policy = ShellPolicy()


def set_shell_policy(policy: ShellPolicy) -> None:
    """Override the global shell policy (for testing or configuration)."""
    global _default_policy
    _default_policy = policy


class RunCommand(Tool):
    def __init__(
        self,
        default_timeout: int = 30,
        policy: ShellPolicy | None = None,
    ) -> None:
        super().__init__(
            name="run_command",
            description=(
                "Execute a shell command and return its output. "
                "Use for running scripts, builds, or system queries. "
                "Output includes stdout, stderr, and exit code. "
                "PRIVILEGED: this can execute arbitrary commands on the host."
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
        self._policy = policy or _default_policy

    def execute(self, command: str = "", timeout: int | None = None, **kwargs) -> str:  # type: ignore[override]
        if not command or not command.strip():
            return "Error: command must not be empty"
        if len(command) > 8000:
            return "Error: command too long (max 8000 chars)"

        # Policy check
        allowed, reason = self._policy.check(command)
        if not allowed:
            logger.warning("Shell policy rejected command: %s — %s", command[:200], reason)
            return f"Error: command rejected by policy: {reason}"

        # Log every invocation (privileged tool)
        logger.info("Shell execution: %s", command[:500])

        if timeout is None:
            timeout = self.default_timeout
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
