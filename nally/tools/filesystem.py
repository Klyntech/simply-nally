"""Filesystem tools — read, write, list. Simple, safe, explicit."""

from __future__ import annotations

import os
from pathlib import Path

from .base import Tool


def _safe_path(path: str) -> tuple[bool, str]:
    if not path or not path.strip():
        return False, "path must not be empty"
    if "\x00" in path:
        return False, "path contains null byte"
    # Prevent absurdly long paths
    if len(path) > 500:
        return False, "path too long"
    return True, ""


class ReadFile(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="read_file",
            description="Read the contents of a text file. Returns the file content or an error.",
            parameters={
                "path": {
                    "type": "string",
                    "description": "Path to the file to read (relative or absolute)",
                    "required": True,
                }
            },
        )

    def execute(self, path: str = "", **kwargs) -> str:  # type: ignore[override]
        ok, err = _safe_path(path)
        if not ok:
            return f"Error: {err}"
        p = Path(path)
        if not p.exists():
            return f"Error: file not found: {path}"
        if not p.is_file():
            return f"Error: not a file: {path}"
        try:
            # Guard against huge files (1 MB limit for text preview)
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
    def __init__(self) -> None:
        super().__init__(
            name="write_file",
            description="Write content to a file. Creates parent directories if needed. Overwrites existing files.",
            parameters={
                "path": {
                    "type": "string",
                    "description": "Path to the file to write",
                    "required": True,
                },
                "content": {
                    "type": "string",
                    "description": "Text content to write",
                    "required": True,
                },
            },
        )

    def execute(self, path: str = "", content: str = "", **kwargs) -> str:  # type: ignore[override]
        ok, err = _safe_path(path)
        if not ok:
            return f"Error: {err}"
        # Content must be string (validated by registry, but double-check)
        if not isinstance(content, str):
            return "Error: content must be a string"
        p = Path(path)
        try:
            # Create parent dirs
            if p.parent and str(p.parent) not in (".", ""):
                p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return f"Wrote {len(content)} chars to {path}"
        except PermissionError:
            return f"Error: permission denied: {path}"
        except OSError as exc:
            return f"Error: could not write {path}: {exc}"


class ListDir(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="list_dir",
            description="List files and directories at a given path. Defaults to current directory.",
            parameters={
                "path": {
                    "type": "string",
                    "description": "Directory path to list (defaults to '.')",
                    "required": False,
                }
            },
        )

    def execute(self, path: str = ".", **kwargs) -> str:  # type: ignore[override]
        # Allow empty -> default to "."
        if not path or not path.strip():
            path = "."
        ok, err = _safe_path(path)
        if not ok:
            return f"Error: {err}"
        p = Path(path)
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


# Convenience to register all filesystem tools
def register_filesystem_tools(registry) -> None:
    registry.register(ReadFile())
    registry.register(WriteFile())
    registry.register(ListDir())
