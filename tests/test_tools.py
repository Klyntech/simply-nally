"""Tests for Tool base, registry, filesystem, shell, and web tools."""

from __future__ import annotations

from pathlib import Path

import pytest
from nally.tools.base import Tool, ToolRegistry, ToolResult
from nally.tools.filesystem import register_filesystem_tools, _resolve_within_workspace, _WorkspaceError
from nally.tools.shell import RunCommand, ShellPolicy


# ---------------------------------------------------------------------------
# Tool base — validation
# ---------------------------------------------------------------------------
class TestToolValidation:
    def setup_method(self):
        self.tool = Tool(
            name="demo_tool",
            description="Demo",
            parameters={
                "path": {"type": "string", "description": "a path", "required": True},
                "count": {"type": "integer", "description": "a count", "required": False},
                "mode": {
                    "type": "string",
                    "description": "mode",
                    "required": False,
                    "enum": ["a", "b"],
                },
            },
        )

    def test_missing_required(self):
        ok, err = self.tool.validate({})
        assert not ok and "path" in err

    def test_unknown_param(self):
        ok, err = self.tool.validate({"path": "x", "extra": 1})
        assert not ok and "unknown" in err

    def test_wrong_type(self):
        ok, err = self.tool.validate({"path": 123})
        assert not ok and "expected type" in err

    def test_integer_rejects_bool(self):
        ok, _err = self.tool.validate({"path": "x", "count": True})
        assert not ok

    def test_enum(self):
        ok, _ = self.tool.validate({"path": "x", "mode": "a"})
        assert ok
        ok, err = self.tool.validate({"path": "x", "mode": "z"})
        assert not ok and "must be one of" in err

    def test_valid(self):
        ok, _ = self.tool.validate({"path": "x", "count": 2})
        assert ok

    def test_schema(self):
        schema = self.tool.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "demo_tool"
        assert "path" in schema["function"]["parameters"]["required"]

    def test_unknown_type_raises(self):
        """Unknown parameter type should raise ValueError at validation time."""
        bad_tool = Tool(
            name="bad",
            description="bad",
            parameters={"x": {"type": "strng", "required": True}},
        )
        with pytest.raises(ValueError, match="Unknown parameter type"):
            bad_tool.validate({"x": "hello"})


# ---------------------------------------------------------------------------
# ToolResult
# ---------------------------------------------------------------------------
class TestToolResult:
    def test_dataclass(self):
        r = ToolResult(output="ok", success=True, tool_name="t")
        assert r.success
        assert r.tool_name == "t"
        assert r.metadata == {}

    def test_success_field(self):
        r = ToolResult(output="Error: something", success=False)
        assert not r.success


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
class TestRegistry:
    def test_register_and_get(self):
        r = ToolRegistry()
        t = Tool(name="t1", description="d", parameters={})
        r.register(t)
        assert r.get("t1") is t
        assert len(r) == 1

    def test_duplicate_raises(self):
        r = ToolRegistry()
        r.register(Tool(name="t1", description="d"))
        with pytest.raises(ValueError, match="already registered"):
            r.register(Tool(name="t1", description="d2"))

    def test_unknown_tool(self):
        r = ToolRegistry()
        result, ok = r.execute("nope", {})
        assert not ok and "unknown tool" in result

    def test_validation_before_execute(self):
        r = ToolRegistry()

        class MyTool(Tool):
            def __init__(self):
                super().__init__("mytool", "d", {"x": {"type": "string", "required": True}})

            def execute(self, x="", **kw):
                return f"got {x}"

        r.register(MyTool())
        result, ok = r.execute("mytool", {})
        assert not ok and "missing required" in result

    def test_truncation(self):
        r = ToolRegistry(max_output=10)

        class BigTool(Tool):
            def __init__(self):
                super().__init__("big", "d", {})

            def execute(self, **kw):
                return "x" * 100

        r.register(BigTool())
        result, ok = r.execute("big", {})
        assert ok
        assert len(result) < 100
        assert "truncated" in result

    def test_non_string_result(self):
        r = ToolRegistry()

        class DictTool(Tool):
            def __init__(self):
                super().__init__("dtool", "d", {})

            def execute(self, **kw):
                return {"a": 1}

        r.register(DictTool())
        result, ok = r.execute("dtool", {})
        assert ok
        assert '"a": 1' in result

    def test_all_schemas(self):
        r = ToolRegistry()
        r.register(Tool(name="a", description="d"))
        r.register(Tool(name="b", description="d2"))
        schemas = r.all_schemas()
        assert len(schemas) == 2

    def test_error_result_not_false_positive(self):
        """A tool returning 'Error rates are...' should NOT be marked as failure."""
        r = ToolRegistry()

        class NormalTool(Tool):
            def __init__(self):
                super().__init__("normal", "d", {})

            def execute(self, **kw):
                return "Error rates are currently 5%"

        r.register(NormalTool())
        result, ok = r.execute("normal", {})
        assert ok  # should be True — this is a normal response, not an error


# ---------------------------------------------------------------------------
# Filesystem — workspace boundary
# ---------------------------------------------------------------------------
class TestWorkspaceBoundary:
    def test_path_within_workspace(self, tmp_path: Path):
        p = _resolve_within_workspace("file.txt", tmp_path)
        assert p == (tmp_path / "file.txt").resolve()

    def test_subdirectory(self, tmp_path: Path):
        p = _resolve_within_workspace("a/b/file.txt", tmp_path)
        assert p == (tmp_path / "a" / "b" / "file.txt").resolve()

    def test_escape_rejected(self, tmp_path: Path):
        with pytest.raises(_WorkspaceError, match="escapes workspace"):
            _resolve_within_workspace("../../etc/passwd", tmp_path)

    def test_absolute_path_rejected(self, tmp_path: Path):
        with pytest.raises(_WorkspaceError, match="escapes workspace"):
            _resolve_within_workspace("/etc/passwd", tmp_path)

    def test_empty_path_rejected(self, tmp_path: Path):
        with pytest.raises(_WorkspaceError, match="must not be empty"):
            _resolve_within_workspace("", tmp_path)

    def test_null_byte_rejected(self, tmp_path: Path):
        with pytest.raises(_WorkspaceError, match="null byte"):
            _resolve_within_workspace("file\x00.txt", tmp_path)


class TestFilesystem:
    def test_write_and_read(self, tmp_path: Path):
        r = ToolRegistry()
        register_filesystem_tools(r, workspace=tmp_path)
        p = tmp_path / "hello.txt"
        result, ok = r.execute("write_file", {"path": str(p), "content": "hello world"})
        assert ok and "Wrote" in result
        result, ok = r.execute("read_file", {"path": str(p)})
        assert ok and result == "hello world"

    def test_read_missing(self, tmp_path: Path):
        r = ToolRegistry()
        register_filesystem_tools(r, workspace=tmp_path)
        result, ok = r.execute("read_file", {"path": str(tmp_path / "missing.txt")})
        assert not ok and "not found" in result

    def test_write_creates_parents(self, tmp_path: Path):
        r = ToolRegistry()
        register_filesystem_tools(r, workspace=tmp_path)
        p = tmp_path / "a" / "b" / "c.txt"
        _result, ok = r.execute("write_file", {"path": str(p), "content": "hi"})
        assert ok
        assert p.exists()

    def test_list_dir(self, tmp_path: Path):
        r = ToolRegistry()
        register_filesystem_tools(r, workspace=tmp_path)
        (tmp_path / "file.txt").write_text("x")
        (tmp_path / "subdir").mkdir()
        result, ok = r.execute("list_dir", {"path": str(tmp_path)})
        assert ok
        assert "file.txt" in result
        assert "subdir/" in result

    def test_list_empty(self, tmp_path: Path):
        r = ToolRegistry()
        register_filesystem_tools(r, workspace=tmp_path)
        empty = tmp_path / "empty"
        empty.mkdir()
        result, ok = r.execute("list_dir", {"path": str(empty)})
        assert ok and "empty" in result.lower()

    def test_list_missing(self, tmp_path: Path):
        r = ToolRegistry()
        register_filesystem_tools(r, workspace=tmp_path)
        _result, ok = r.execute("list_dir", {"path": str(tmp_path / "nope")})
        assert not ok

    def test_read_large_file(self, tmp_path: Path):
        r = ToolRegistry()
        register_filesystem_tools(r, workspace=tmp_path)
        p = tmp_path / "big.txt"
        p.write_bytes(b"x" * 1_100_000)
        result, ok = r.execute("read_file", {"path": str(p)})
        assert not ok and "too large" in result

    def test_empty_path(self, tmp_path: Path):
        r = ToolRegistry()
        register_filesystem_tools(r, workspace=tmp_path)
        _result, ok = r.execute("read_file", {"path": ""})
        assert not ok


# ---------------------------------------------------------------------------
# Shell — policy
# ---------------------------------------------------------------------------
class TestShellPolicy:
    def test_default_policy_allows_normal(self):
        policy = ShellPolicy()
        allowed, _ = policy.check("ls -la")
        assert allowed

    def test_default_policy_blocks_rm_rf_root(self):
        policy = ShellPolicy()
        allowed, reason = policy.check("rm -rf /")
        assert not allowed
        assert "blocked" in reason.lower()

    def test_default_policy_blocks_mkfs(self):
        policy = ShellPolicy()
        allowed, _ = policy.check("mkfs.ext4 /dev/sda1")
        assert not allowed

    def test_policy_logs_command(self):
        """Verify ShellTool logs commands (just check it doesn't crash)."""
        tool = RunCommand()
        result = tool.execute(command="echo test", timeout=5)
        assert "test" in result


class TestShell:
    def test_run_command_success(self):
        r = ToolRegistry()
        r.register(RunCommand())
        result, ok = r.execute("run_command", {"command": "python -c \"print('hi')\""})
        assert ok
        assert "hi" in result
        assert "Exit code: 0" in result

    def test_run_command_failure(self):
        r = ToolRegistry()
        r.register(RunCommand())
        result, ok = r.execute("run_command", {"command": 'python -c "import sys; sys.exit(1)"'})
        assert ok  # tool itself succeeded; command exit code is part of output
        assert "Exit code: 1" in result

    def test_empty_command(self):
        r = ToolRegistry()
        r.register(RunCommand())
        _result, ok = r.execute("run_command", {"command": ""})
        assert not ok

    def test_timeout_clamped(self):
        t = RunCommand()
        assert t.validate({"command": "echo hi", "timeout": 5})[0]
        ok, err = t.validate({"command": "echo hi", "timeout": "bad"})
        assert not ok
        assert "expected type" in err


# ---------------------------------------------------------------------------
# Think tool removed
# ---------------------------------------------------------------------------
class TestThinkRemoved:
    def test_think_not_in_default_registry(self):
        from nally.tools import build_default_registry
        r = build_default_registry(load_mcp=False)
        assert "think" not in r.names()
