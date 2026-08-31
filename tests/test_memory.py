"""Tests for memory layer — models, store, manager, tools (mocked, no real DB)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from nally.memory.manager import MemoryManager
from nally.memory.models import MemoryRecord, MemoryStoreError, MemoryType
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
# Helpers
# ---------------------------------------------------------------------------


def _mock_row(**overrides):
    """Create a mock fact row (no confidence column)."""
    base = {
        "id": "uuid-1",
        "user_id": "user-1",
        "type": "preference",
        "key": "editor",
        "value": "VS Code",
        "source": "user",
        "created_at": None,
        "updated_at": None,
    }
    base.update(overrides)
    return base


def _mem(**overrides) -> MemoryRecord:
    """Create a MemoryRecord quickly."""
    defaults = {
        "id": "uuid-1",
        "user_id": "user-1",
        "type": MemoryType.FACT,
        "key": "name",
        "value": "Alex",
    }
    defaults.update(overrides)
    return MemoryRecord(**defaults)


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
        assert str(MemoryType.PREFERENCE) == "preference"
        assert MemoryType("preference") == MemoryType.PREFERENCE


class TestMemoryStoreError:
    def test_is_exception(self):
        assert issubclass(MemoryStoreError, Exception)

    def test_message(self):
        err = MemoryStoreError("something broke")
        assert str(err) == "something broke"


class TestMemoryRecord:
    def test_from_row(self):
        row = _mock_row(
            id="uuid-1",
            user_id="user-1",
            type="preference",
            key="programming_language",
            value="TypeScript",
        )
        record = MemoryRecord.from_row(row)
        assert record.id == "uuid-1"
        assert record.user_id == "user-1"
        assert record.type == MemoryType.PREFERENCE
        assert record.key == "programming_language"
        assert record.value == "TypeScript"
        assert record.source == "user"

    def test_to_context_line(self):
        record = _mem(type=MemoryType.PREFERENCE, key="editor", value="VS Code")
        assert record.to_context_line() == "- editor: VS Code (preference)"

    def test_no_confidence_attribute(self):
        record = _mem()
        assert not hasattr(record, "confidence")


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


class TestMemoryStore:
    def test_remember_calls_upsert_fact(self):
        store = MemoryStore("user-1")
        with patch("nally.db.pooled_connect") as mock_connect, \
             patch("nally.db.upsert_fact", return_value=_mock_row()) as mock_upsert:
            mock_connect.return_value = MagicMock()
            result = store.remember("editor", "VS Code", type=MemoryType.PREFERENCE)
            assert result is not None
            assert result.key == "editor"
            mock_upsert.assert_called_once()

    def test_remember_raises_on_db_error(self):
        store = MemoryStore("user-1")
        with patch("nally.db.pooled_connect", side_effect=RuntimeError("DB down")), \
             pytest.raises(MemoryStoreError):
            store.remember("key", "value")

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

    def test_recall_raises_on_db_error(self):
        store = MemoryStore("user-1")
        with patch("nally.db.pooled_connect", side_effect=RuntimeError("DB down")), \
             pytest.raises(MemoryStoreError):
            store.recall("key")

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

    def test_store_requires_user_id(self):
        with pytest.raises(ValueError):
            MemoryStore("")

    def test_remember_truncates_long_value(self):
        store = MemoryStore("user-1")
        with patch("nally.db.pooled_connect") as mock_connect, \
             patch("nally.db.upsert_fact", return_value=_mock_row()) as mock_upsert:
            mock_connect.return_value = MagicMock()
            long_value = "x" * 1000
            store.remember("key", long_value)
            # Value should be truncated to 500 chars
            call_kwargs = mock_upsert.call_args[1]
            assert len(call_kwargs["value"]) <= 500


# ---------------------------------------------------------------------------
# Store error handling
# ---------------------------------------------------------------------------


class TestStoreErrorHandling:
    """Verify that infrastructure failures raise MemoryStoreError, not silent None."""

    def test_remember_db_failure_raises(self):
        store = MemoryStore("user-1")
        with patch("nally.db.pooled_connect", side_effect=RuntimeError("timeout")), \
             pytest.raises(MemoryStoreError, match="remember failed"):
            store.remember("k", "v")

    def test_recall_db_failure_raises(self):
        store = MemoryStore("user-1")
        with patch("nally.db.pooled_connect", side_effect=RuntimeError("timeout")), \
             pytest.raises(MemoryStoreError, match="recall failed"):
            store.recall("k")

    def test_search_db_failure_raises(self):
        store = MemoryStore("user-1")
        with patch("nally.db.pooled_connect", side_effect=RuntimeError("timeout")), \
             pytest.raises(MemoryStoreError, match="search failed"):
            store.search("q")

    def test_forget_db_failure_raises(self):
        store = MemoryStore("user-1")
        with patch("nally.db.pooled_connect", side_effect=RuntimeError("timeout")), \
             pytest.raises(MemoryStoreError, match="forget failed"):
            store.forget("k")

    def test_list_all_db_failure_raises(self):
        store = MemoryStore("user-1")
        with patch("nally.db.pooled_connect", side_effect=RuntimeError("timeout")), \
             pytest.raises(MemoryStoreError, match="list_all failed"):
            store.list_all()


# ---------------------------------------------------------------------------
# Cross-user isolation
# ---------------------------------------------------------------------------


class TestCrossUserIsolation:
    def test_users_have_separate_stores(self):
        store_a = MemoryStore("user-a")
        store_b = MemoryStore("user-b")
        assert store_a.user_id != store_b.user_id

    def test_remember_scoped_to_user(self):
        store_a = MemoryStore("user-a")
        with patch("nally.db.pooled_connect") as mock_connect, \
             patch("nally.db.upsert_fact") as mock_upsert:
            mock_connect.return_value = MagicMock()
            mock_upsert.return_value = _mock_row(user_id="user-a", key="lang", value="Python")
            store_a.remember("lang", "Python", type=MemoryType.PREFERENCE)
            # Verify user_id was passed correctly
            call_args = mock_upsert.call_args
            assert call_args[0][1] == "user-a"  # user_id is 2nd positional arg

    def test_forget_does_not_affect_other_users(self):
        store_a = MemoryStore("user-a")
        with patch("nally.db.pooled_connect") as mock_connect, \
             patch("nally.db.delete_fact", return_value=True) as mock_delete:
            mock_connect.return_value = MagicMock()
            store_a.forget("lang")
            call_args = mock_delete.call_args
            assert call_args[0][1] == "user-a"  # user_id


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
        mock_record = _mem(type=MemoryType.PREFERENCE, key="editor", value="VS Code")
        with patch.object(mm._store, "remember", return_value=mock_record) as mock_remember:
            response = mm.handle_memory_command("remember that I prefer VS Code")
            assert response is not None
            assert "Remembered" in response
            mock_remember.assert_called_once()

    def test_handle_remember_db_failure(self):
        mm = MemoryManager("user-1")
        with patch.object(mm._store, "remember", side_effect=MemoryStoreError("DB down")):
            response = mm.handle_memory_command("remember that I prefer VS Code")
            assert "storage unavailable" in response

    def test_handle_forget_command(self):
        mm = MemoryManager("user-1")
        mock_record = _mem(type=MemoryType.PREFERENCE, key="editor", value="VS Code")
        with (
            patch.object(mm._store, "recall", return_value=mock_record),
            patch.object(mm._store, "forget", return_value=True) as mock_forget,
        ):
            response = mm.handle_memory_command("forget editor")
            assert response is not None
            assert "Forgot" in response
            mock_forget.assert_called_once_with("editor")

    def test_handle_forget_no_typo(self):
        """Regression: response should say 'Forgot', not 'Forget'."""
        mm = MemoryManager("user-1")
        mock_record = _mem(type=MemoryType.PREFERENCE, key="x", value="y")
        with (
            patch.object(mm._store, "recall", return_value=None),
            patch.object(mm._store, "search", return_value=[mock_record]),
            patch.object(mm._store, "forget", return_value=True),
        ):
            response = mm.handle_memory_command("forget x")
            assert response.startswith("Forgot:")

    def test_handle_recall_command(self):
        mm = MemoryManager("user-1")
        memories = [_mem(type=MemoryType.FACT, key="name", value="Alex")]
        with patch.object(mm._store, "list_all", return_value=memories):
            response = mm.handle_memory_command("what do you remember about me")
            assert response is not None
            assert "name" in response
            assert "Alex" in response

    def test_handle_recall_db_failure(self):
        mm = MemoryManager("user-1")
        with patch.object(mm._store, "list_all", side_effect=MemoryStoreError("DB down")):
            response = mm.handle_memory_command("what do you remember about me")
            assert "storage unavailable" in response

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
        with patch.object(mm._store, "list_all", return_value=[]):
            context = mm.build_context_block("hello")
            assert context == ""

    def test_build_context_block_includes_memories(self):
        mm = MemoryManager("user-1")
        memories = [_mem(type=MemoryType.PREFERENCE, key="editor", value="VS Code")]
        with patch.object(mm._store, "list_all", return_value=memories):
            context = mm.build_context_block("what editor do I use?")
            assert "Known Facts" in context
            assert "editor: VS Code" in context

    def test_build_context_block_respects_char_limit(self):
        mm = MemoryManager("user-1")
        big_value = "x" * 3000
        memories = [_mem(key="big", value=big_value)]
        with patch.object(mm._store, "list_all", return_value=memories):
            context = mm.build_context_block("tell me about big")
            assert len(context) <= 2100  # some overhead for header
            assert "truncated" in context

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
        assert key == "code_editor"
        assert value == "VS Code daily"
        assert mem_type == MemoryType.PREFERENCE


# ---------------------------------------------------------------------------
# Retrieval ranking
# ---------------------------------------------------------------------------


class TestRetrievalRanking:
    def test_exact_key_match_scores_highest(self):
        mm = MemoryManager("user-1")
        memories = [
            _mem(key="editor", value="VS Code", type=MemoryType.PREFERENCE),
            _mem(id="2", key="language", value="TypeScript", type=MemoryType.PREFERENCE),
        ]
        with patch.object(mm._store, "list_all", return_value=memories):
            results = mm.get_relevant_memories("what is my editor")
            assert len(results) > 0
            assert results[0].key == "editor"

    def test_value_match_retrieves(self):
        mm = MemoryManager("user-1")
        memories = [
            _mem(key="language", value="TypeScript", type=MemoryType.PREFERENCE),
        ]
        with patch.object(mm._store, "list_all", return_value=memories):
            results = mm.get_relevant_memories("help me with TypeScript")
            assert len(results) == 1
            assert results[0].key == "language"

    def test_no_match_returns_empty(self):
        mm = MemoryManager("user-1")
        memories = [
            _mem(key="language", value="TypeScript", type=MemoryType.PREFERENCE),
        ]
        with patch.object(mm._store, "list_all", return_value=memories):
            results = mm.get_relevant_memories("hello")
            assert len(results) == 0

    def test_db_failure_returns_empty(self):
        mm = MemoryManager("user-1")
        with patch.object(mm._store, "list_all", side_effect=MemoryStoreError("DB down")):
            results = mm.get_relevant_memories("TypeScript")
            assert results == []

    def test_instruction_type_always_relevant(self):
        mm = MemoryManager("user-1")
        memories = [
            _mem(key="always_do", value="be concise", type=MemoryType.INSTRUCTION),
        ]
        with patch.object(mm._store, "list_all", return_value=memories):
            results = mm.get_relevant_memories("tell me a joke")
            # Instructions get a base score, should appear
            assert len(results) == 1


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class TestMemoryTools:
    def test_remember_tool_execute(self):
        store = MagicMock(spec=MemoryStore)
        store.remember.return_value = _mem(type=MemoryType.FACT, key="editor", value="VS Code")
        tool = RememberTool(store)
        result = tool.execute(key="editor", value="VS Code", type="fact")
        assert "Remembered" in result
        assert "editor" in result

    def test_remember_tool_db_error(self):
        store = MagicMock(spec=MemoryStore)
        store.remember.side_effect = MemoryStoreError("DB down")
        tool = RememberTool(store)
        result = tool.execute(key="editor", value="VS Code")
        assert "unavailable" in result

    def test_remember_tool_invalid_type(self):
        store = MagicMock(spec=MemoryStore)
        tool = RememberTool(store)
        result = tool.execute(key="k", value="v", type="not_real")
        assert "invalid" in result

    def test_remember_tool_validation(self):
        store = MagicMock(spec=MemoryStore)
        tool = RememberTool(store)
        ok, err = tool.validate({"key": "editor", "value": "VS Code"})
        assert ok is True
        ok, err = tool.validate({"value": "VS Code"})
        assert ok is False
        assert "key" in err

    def test_recall_tool_execute(self):
        store = MagicMock(spec=MemoryStore)
        store.recall.return_value = _mem(key="name", value="Alex")
        tool = RecallTool(store)
        result = tool.execute(key="name")
        assert "name" in result
        assert "Alex" in result

    def test_recall_tool_not_found(self):
        store = MagicMock(spec=MemoryStore)
        store.recall.return_value = None
        tool = RecallTool(store)
        result = tool.execute(key="nonexistent")
        assert "No memory" in result

    def test_recall_tool_db_error(self):
        store = MagicMock(spec=MemoryStore)
        store.recall.side_effect = MemoryStoreError("DB down")
        tool = RecallTool(store)
        result = tool.execute(key="k")
        assert "unavailable" in result

    def test_forget_tool_execute(self):
        store = MagicMock(spec=MemoryStore)
        store.recall.return_value = _mem(key="editor", value="VS Code")
        store.forget.return_value = True
        tool = ForgetTool(store)
        result = tool.execute(key="editor")
        assert "Forgot" in result

    def test_forget_tool_not_found(self):
        store = MagicMock(spec=MemoryStore)
        store.recall.return_value = None
        tool = ForgetTool(store)
        result = tool.execute(key="nope")
        assert "No memory" in result

    def test_search_memory_tool_execute(self):
        store = MagicMock(spec=MemoryStore)
        store.search.return_value = [_mem(key="editor", value="VS Code")]
        tool = SearchMemoryTool(store)
        result = tool.execute(query="VS")
        assert "Found 1" in result
        assert "editor" in result

    def test_search_memory_tool_no_results(self):
        store = MagicMock(spec=MemoryStore)
        store.search.return_value = []
        tool = SearchMemoryTool(store)
        result = tool.execute(query="nothing")
        assert "No memories" in result

    def test_search_memory_tool_db_error(self):
        store = MagicMock(spec=MemoryStore)
        store.search.side_effect = MemoryStoreError("DB down")
        tool = SearchMemoryTool(store)
        result = tool.execute(query="q")
        assert "unavailable" in result

    def test_list_memories_tool_execute(self):
        store = MagicMock(spec=MemoryStore)
        store.list_all.return_value = [_mem(key="name", value="Alex")]
        tool = ListMemoriesTool(store)
        result = tool.execute()
        assert "Stored memories (1)" in result
        assert "name" in result

    def test_list_memories_tool_empty(self):
        store = MagicMock(spec=MemoryStore)
        store.list_all.return_value = []
        tool = ListMemoriesTool(store)
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
