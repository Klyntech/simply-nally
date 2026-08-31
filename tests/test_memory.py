"""Tests for memory layer — models, store, manager, tools (mocked, no real DB)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from nally.memory.manager import MemoryManager
from nally.memory.models import MemoryRecord, MemoryType
from nally.memory.store import MemoryStore
from nally.tools.memory import (
    ForgetTool,
    ListMemoriesTool,
    RecallTool,
    RememberTool,
    SearchMemoryTool,
    register_memory_tools,
)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TestMemoryType:
    def test_enum_values(self):
        assert MemoryType.PREFERENCE.value == "preference"
        assert MemoryType.FACT.value == "fact"
        assert MemoryType.INSTRUCTION.value == "instruction"
        assert MemoryType.PROFILE.value == "profile"
        assert MemoryType.PROJECT.value == "project"

    def test_str_enum(self):
        # StrEnum: str() returns the value, not the class-qualified name
        assert str(MemoryType.PREFERENCE) == "preference"
        assert MemoryType("preference") == MemoryType.PREFERENCE


class TestMemoryRecord:
    def test_from_row(self):
        row = {
            "id": "uuid-1",
            "user_id": "user-1",
            "type": "preference",
            "key": "programming_language",
            "value": "TypeScript",
            "source": "user",
            "confidence": 1.0,
            "created_at": datetime(2025, 1, 1, tzinfo=UTC),
            "updated_at": datetime(2025, 1, 2, tzinfo=UTC),
        }
        record = MemoryRecord.from_row(row)
        assert record.id == "uuid-1"
        assert record.user_id == "user-1"
        assert record.type == MemoryType.PREFERENCE
        assert record.key == "programming_language"
        assert record.value == "TypeScript"
        assert record.source == "user"
        assert record.confidence == 1.0

    def test_to_context_line(self):
        record = MemoryRecord(
            id="1",
            user_id="u1",
            type=MemoryType.PREFERENCE,
            key="editor",
            value="VS Code",
        )
        assert record.to_context_line() == "- editor: VS Code (preference)"


# ---------------------------------------------------------------------------
# Key normalization
# ---------------------------------------------------------------------------


class TestKeyNormalization:
    def test_normalize_key_lowercase(self):
        from nally.db import _normalize_key

        assert _normalize_key("Programming Language") == "programming_language"

    def test_normalize_key_spaces(self):
        from nally.db import _normalize_key

        assert _normalize_key("  my  key  ") == "my_key"

    def test_normalize_key_special_chars(self):
        from nally.db import _normalize_key

        assert _normalize_key("my-key!@#") == "my_key"

    def test_normalize_key_collapse_underscores(self):
        from nally.db import _normalize_key

        assert _normalize_key("a__b___c") == "a_b_c"

    def test_normalize_key_max_length(self):
        from nally.db import _normalize_key

        long_key = "a" * 100
        result = _normalize_key(long_key)
        assert len(result) == 64


# ---------------------------------------------------------------------------
# Store (mocked DB via nally.db module)
# ---------------------------------------------------------------------------


def _mock_row(**overrides):
    """Create a mock fact row."""
    base = {
        "id": "uuid-1",
        "user_id": "user-1",
        "type": "preference",
        "key": "editor",
        "value": "VS Code",
        "source": "user",
        "confidence": 1.0,
        "created_at": None,
        "updated_at": None,
    }
    base.update(overrides)
    return base


class TestMemoryStore:
    def test_remember_calls_upsert_fact(self):
        store = MemoryStore("user-1")
        with patch("nally.db.pooled_connect") as mock_connect, \
             patch("nally.db.upsert_fact", return_value=_mock_row()) as mock_upsert:
            mock_conn = MagicMock()
            mock_connect.return_value = mock_conn
            result = store.remember("editor", "VS Code", type="preference")
            assert result is not None
            assert result.key == "editor"
            mock_upsert.assert_called_once()

    def test_recall_calls_get_fact(self):
        store = MemoryStore("user-1")
        with patch("nally.db.pooled_connect") as mock_connect, \
             patch("nally.db.get_fact", return_value=_mock_row(key="name", value="Alex")):
            mock_connect.return_value = MagicMock()
            result = store.recall("name")
            assert result is not None
            assert result.value == "Alex"

    def test_recall_returns_none_when_not_found(self):
        store = MemoryStore("user-1")
        with patch("nally.db.pooled_connect") as mock_connect, \
             patch("nally.db.get_fact", return_value=None):
            mock_connect.return_value = MagicMock()
            result = store.recall("nonexistent")
            assert result is None

    def test_forget_calls_delete_fact(self):
        store = MemoryStore("user-1")
        with patch("nally.db.pooled_connect") as mock_connect, \
             patch("nally.db.delete_fact", return_value=True):
            mock_connect.return_value = MagicMock()
            assert store.forget("editor") is True

    def test_search_returns_records(self):
        store = MemoryStore("user-1")
        with patch("nally.db.pooled_connect") as mock_connect, \
             patch("nally.db.search_facts", return_value=[_mock_row()]):
            mock_connect.return_value = MagicMock()
            results = store.search("VS")
            assert len(results) == 1
            assert results[0].key == "editor"

    def test_list_all_returns_records(self):
        store = MemoryStore("user-1")
        with patch("nally.db.pooled_connect") as mock_connect, \
             patch("nally.db.list_facts", return_value=[_mock_row(key="name", value="Alex")]):
            mock_connect.return_value = MagicMock()
            results = store.list_all()
            assert len(results) == 1

    def test_store_handles_db_error(self):
        store = MemoryStore("user-1")
        with patch("nally.db.pooled_connect", side_effect=RuntimeError("DB unavailable")):
            result = store.remember("key", "value")
            assert result is None

    def test_store_handles_missing_user_id(self):
        store = MemoryStore("")
        with patch("nally.db.pooled_connect", side_effect=RuntimeError("No user")):
            result = store.remember("key", "value")
            assert result is None


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class TestMemoryManager:
    def test_enabled_with_user_id(self):
        mm = MemoryManager("user-1")
        assert mm.enabled is True

    def test_disabled_without_user_id(self):
        mm = MemoryManager("")
        assert mm.enabled is False

    def test_handle_remember_command(self):
        mm = MemoryManager("user-1")
        mock_record = MemoryRecord(
            id="1", user_id="user-1", type=MemoryType.PREFERENCE,
            key="editor", value="VS Code",
        )
        with patch.object(mm._store, "remember", return_value=mock_record) as mock_remember:
            response = mm.handle_memory_command("remember that I prefer VS Code")
            assert response is not None
            assert "Remembered" in response
            mock_remember.assert_called_once()

    def test_handle_forget_command(self):
        mm = MemoryManager("user-1")
        mock_record = MemoryRecord(
            id="1", user_id="user-1", type=MemoryType.PREFERENCE,
            key="editor", value="VS Code",
        )
        with (
            patch.object(mm._store, "recall", return_value=mock_record),
            patch.object(mm._store, "forget", return_value=True) as mock_forget,
        ):
            response = mm.handle_memory_command("forget editor")
            assert response is not None
            assert "Forgot" in response
            mock_forget.assert_called_once_with("editor")

    def test_handle_recall_command(self):
        mm = MemoryManager("user-1")
        memories = [
            MemoryRecord(id="1", user_id="user-1", type=MemoryType.FACT, key="name", value="Alex"),
        ]
        with patch.object(mm._store, "list_all", return_value=memories):
            response = mm.handle_memory_command("what do you remember about me")
            assert response is not None
            assert "name" in response
            assert "Alex" in response

    def test_handle_non_memory_command(self):
        mm = MemoryManager("user-1")
        response = mm.handle_memory_command("hello, how are you?")
        assert response is None

    def test_disabled_manager_returns_none(self):
        mm = MemoryManager("")
        response = mm.handle_memory_command("remember that I like Python")
        assert response is None

    def test_build_context_block_empty_when_no_memories(self):
        mm = MemoryManager("user-1")
        with patch.object(mm._store, "search", return_value=[]):
            context = mm.build_context_block("hello")
            assert context == ""

    def test_build_context_block_includes_memories(self):
        mm = MemoryManager("user-1")
        memories = [
            MemoryRecord(
                id="1", user_id="user-1", type=MemoryType.PREFERENCE,
                key="editor", value="VS Code",
            ),
        ]
        with patch.object(mm._store, "search", return_value=memories):
            context = mm.build_context_block("what editor do I use?")
            assert "Known Facts" in context
            assert "editor: VS Code" in context

    def test_build_context_block_disabled(self):
        mm = MemoryManager("")
        context = mm.build_context_block("hello")
        assert context == ""

    def test_parse_statement_prefers(self):
        mm = MemoryManager("user-1")
        key, value, mem_type = mm._parse_statement("I prefer TypeScript")
        assert key == "programming_language"
        assert value == "TypeScript"
        assert mem_type == MemoryType.PREFERENCE

    def test_parse_statement_my_x_is_y(self):
        mm = MemoryManager("user-1")
        key, value, mem_type = mm._parse_statement("My name is Alex")
        assert key == "name"
        assert value == "Alex"
        assert mem_type == MemoryType.PROFILE

    def test_parse_statement_fallback(self):
        mm = MemoryManager("user-1")
        key, value, mem_type = mm._parse_statement("I use VS Code daily")
        # "VS Code" matches code_editor hint → key becomes "code_editor"
        assert key == "code_editor"
        assert value == "VS Code daily"
        assert mem_type == MemoryType.PREFERENCE


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class TestMemoryTools:
    def test_remember_tool_execute(self):
        tool = RememberTool("user-1")
        mock_record = MemoryRecord(
            id="1", user_id="user-1", type=MemoryType.FACT,
            key="editor", value="VS Code",
        )
        with patch("nally.memory.store.MemoryStore") as MockStore:
            MockStore.return_value.remember.return_value = mock_record
            result = tool.execute(key="editor", value="VS Code", type="fact")
            assert "Remembered" in result
            assert "editor" in result

    def test_remember_tool_validation(self):
        tool = RememberTool("user-1")
        ok, err = tool.validate({"key": "editor", "value": "VS Code"})
        assert ok is True
        ok, err = tool.validate({"value": "VS Code"})
        assert ok is False
        assert "key" in err

    def test_recall_tool_execute(self):
        tool = RecallTool("user-1")
        mock_record = MemoryRecord(
            id="1", user_id="user-1", type=MemoryType.FACT,
            key="name", value="Alex",
        )
        with patch("nally.memory.store.MemoryStore") as MockStore:
            MockStore.return_value.recall.return_value = mock_record
            result = tool.execute(key="name")
            assert "name" in result
            assert "Alex" in result

    def test_recall_tool_not_found(self):
        tool = RecallTool("user-1")
        with patch("nally.memory.store.MemoryStore") as MockStore:
            MockStore.return_value.recall.return_value = None
            result = tool.execute(key="nonexistent")
            assert "No memory" in result

    def test_forget_tool_execute(self):
        tool = ForgetTool("user-1")
        mock_record = MemoryRecord(
            id="1", user_id="user-1", type=MemoryType.FACT,
            key="editor", value="VS Code",
        )
        with patch("nally.memory.store.MemoryStore") as MockStore:
            MockStore.return_value.recall.return_value = mock_record
            MockStore.return_value.forget.return_value = True
            result = tool.execute(key="editor")
            assert "Forgot" in result

    def test_search_memory_tool_execute(self):
        tool = SearchMemoryTool("user-1")
        memories = [
            MemoryRecord(
                id="1", user_id="user-1", type=MemoryType.PREFERENCE,
                key="editor", value="VS Code",
            ),
        ]
        with patch("nally.memory.store.MemoryStore") as MockStore:
            MockStore.return_value.search.return_value = memories
            result = tool.execute(query="VS")
            assert "Found 1" in result
            assert "editor" in result

    def test_search_memory_tool_no_results(self):
        tool = SearchMemoryTool("user-1")
        with patch("nally.memory.store.MemoryStore") as MockStore:
            MockStore.return_value.search.return_value = []
            result = tool.execute(query="nothing")
            assert "No memories" in result

    def test_list_memories_tool_execute(self):
        tool = ListMemoriesTool("user-1")
        memories = [
            MemoryRecord(
                id="1", user_id="user-1", type=MemoryType.FACT,
                key="name", value="Alex",
            ),
        ]
        with patch("nally.memory.store.MemoryStore") as MockStore:
            MockStore.return_value.list_all.return_value = memories
            result = tool.execute()
            assert "Stored memories (1)" in result
            assert "name" in result

    def test_list_memories_tool_empty(self):
        tool = ListMemoriesTool("user-1")
        with patch("nally.memory.store.MemoryStore") as MockStore:
            MockStore.return_value.list_all.return_value = []
            result = tool.execute()
            assert "No memories" in result


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


class TestToolRegistration:
    def test_register_memory_tools(self):
        from nally.tools.base import ToolRegistry

        registry = ToolRegistry()
        register_memory_tools(registry, "user-1")
        assert "remember" in registry
        assert "recall" in registry
        assert "search_memory" in registry
        assert "forget" in registry
        assert "list_memories" in registry
        assert len(registry) == 5

    def test_register_memory_tools_produces_valid_schemas(self):
        from nally.tools.base import ToolRegistry

        registry = ToolRegistry()
        register_memory_tools(registry, "user-1")
        schemas = registry.all_schemas()
        assert len(schemas) == 5
        for schema in schemas:
            assert schema["type"] == "function"
            assert "name" in schema["function"]
            assert "parameters" in schema["function"]
