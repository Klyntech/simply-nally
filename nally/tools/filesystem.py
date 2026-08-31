"""Filesystem tools — read, write, list. Workspace-boundaried, explicit.

All paths are resolved against a workspace root.  The invariant::

    resolved_path.resolve() must be within workspace.resolve()

Symlink escapes are caught by resolve().  The workspace is set once at
construction time (defaults to cwd) and cannot be changed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .base import Tool

logger = logging.getLogger(__name__)


class _WorkspaceError(Exception):
    """Raised when a path escapes the workspace."""


def _resolve_within_workspace(path: str, workspace: Path) -> Path:
    """Resolve *path* and ensure it lives within *workspace*.

    Raises _WorkspaceError on escape.
    """
    if not path or not path.strip():
        raise _WorkspaceError("path must not be empty")
    if "\x00" in path:
        raise _WorkspaceError("path contains null byte")
    if len(path) > 500:
        raise _WorkspaceError("path too long")

    resolved = (workspace / path).resolve()
    ws_resolved = workspace.resolve()

    # The resolved path must start with the workspace root.
    # On Windows, str comparison is case-insensitive for drive letters.
    try:
        resolved.relative_to(ws_resolved)
    except ValueError:
        raise _WorkspaceError(
            f"path escapes workspace: {path!r} resolves to {resolved}, "
            f"workspace is {ws_resolved}"
        ) from None
    return resolved


class ReadFile(Tool):
    def __init__(self, workspace: Path | None = None) -> None:
        super().__init__(
            name="read_file",
            description="Read the contents of a text file within the workspace.",
            parameters={
                "path": {
                    "type": "string",
                    "description": "Relative path within workspace",
                    "required": True,
                }
            },
        )
        self.workspace = (workspace or Path.cwd()).resolve()

    def execute(self, path: str = "", **kwargs) -> str:  # type: ignore[override]
        try:
            p = _resolve_within_workspace(path, self.workspace)
        except _WorkspaceError as exc:
            return f"Error: {exc}"

        if not p.exists():
            return f"Error: file not found: {path}"
        if not p.is_file():
            return f"Error: not a file: {path}"
        try:
            if p.stat().st_size > 1_000_000:
                return f"Error: file too large ({p.stat().st_size} bytes) — limit is 1MB"
            return p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"Error: file is not valid UTF-8 text: {path}"
        except PermissionError:
            return f"Error: permission denied: {path}"
        except OSError as exc:
            return f"Error: could not read {path}: {exc}"


class WriteFile(Tool):
    def __init__(self, workspace: Path | None = None) -> None:
        super().__init__(
            name="write_file",
            description="Write content to a file within the workspace. Creates parent dirs. Overwrites existing files.",
            parameters={
                "path": {
                    "type": "string",
                    "description": "Relative path within workspace",
                    "required": True,
                },
                "content": {
                    "type": "string",
                    "description": "Text content to write",
                    "required": True,
                },
            },
        )
        self.workspace = (workspace or Path.cwd()).resolve()

    def execute(self, path: str = "", content: str = "", **kwargs) -> str:  # type: ignore[override]
        try:
            p = _resolve_within_workspace(path, self.workspace)
        except _WorkspaceError as exc:
            return f"Error: {exc}"

        if not isinstance(content, str):
            return "Error: content must be a string"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return f"Wrote {len(content)} chars to {path}"
        except PermissionError:
            return f"Error: permission denied: {path}"
        except OSError as exc:
            return f"Error: could not write {path}: {exc}"


class ListDir(Tool):
    def __init__(self, workspace: Path | None = None) -> None:
        super().__init__(
            name="list_dir",
            description="List files and directories within the workspace.",
            parameters={
                "path": {
                    "type": "string",
                    "description": "Relative directory path (defaults to workspace root)",
                    "required": False,
                }
            },
        )
        self.workspace = (workspace or Path.cwd()).resolve()

    def execute(self, path: str = ".", **kwargs) -> str:  # type: ignore[override]
        if not path or not path.strip():
            path = "."
        try:
            p = _resolve_within_workspace(path, self.workspace)
        except _WorkspaceError as exc:
            return f"Error: {exc}"

        if not p.exists():
            return f"Error: path not found: {path}"
        if not p.is_dir():
            return f"Error: not a directory: {path}"
        try:
            entries = sorted(os.listdir(p), key=str.lower)
            if not entries:
                return f"Directory {path} is empty"
            lines: list[str] = []
            for name in entries:
                full = p / name
                suffix = "/" if full.is_dir() else ""
                lines.append(f"{name}{suffix}")
            return "\n".join(lines)
        except PermissionError:
            return f"Error: permission denied: {path}"
        except OSError as exc:
            return f"Error: could not list {path}: {exc}"


def register_filesystem_tools(registry, workspace: Path | None = None) -> None:
    """Register filesystem tools bound to *workspace* (defaults to cwd)."""
    ws = (workspace or Path.cwd()).resolve()
    registry.register(ReadFile(workspace=ws))
    registry.register(WriteFile(workspace=ws))
    registry.register(ListDir(workspace=ws))
