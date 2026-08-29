"""Tests for NEON persistence + Google OAuth (mocked, no real DB or browser)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from nally import auth as auth_mod
from nally import db as db_mod
from nally import session as sess_mod
from nally.agent import Agent


class TestDBHelpers:
    def test_get_database_url_empty_when_not_set(self, monkeypatch):
        for k in ("DATABASE_URL", "NALLY_DATABASE_URL", "NEON_DATABASE_URL"):
            monkeypatch.delenv(k, raising=False)
        assert db_mod.get_database_url() == ""
        assert db_mod.is_configured() is False

    def test_get_database_url_prefers_DATABASE_URL(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://a")
        monkeypatch.setenv("NALLY_DATABASE_URL", "postgresql://b")
        assert db_mod.get_database_url() == "postgresql://a"

    def test_get_database_url_fallback(self, monkeypatch):
        for k in ("DATABASE_URL", "NALLY_DATABASE_URL", "NEON_DATABASE_URL"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("NALLY_DATABASE_URL", "postgresql://nally")
        assert db_mod.get_database_url() == "postgresql://nally"

    def test_schema_sql_contains_expected_tables(self):
        sql = db_mod.SCHEMA_SQL
        assert "CREATE TABLE IF NOT EXISTS users" in sql
        assert "CREATE TABLE IF NOT EXISTS sessions" in sql
        assert "CREATE TABLE IF NOT EXISTS messages" in sql
        assert "idx_messages_session_seq" in sql
        assert "gen_random_uuid()" in sql

    def test_connect_raises_when_not_configured(self, monkeypatch):
        for k in ("DATABASE_URL", "NALLY_DATABASE_URL", "NEON_DATABASE_URL"):
            monkeypatch.delenv(k, raising=False)
        with pytest.raises(RuntimeError, match="DATABASE_URL not set"):
            db_mod.connect()

    def test_init_schema_uses_connection(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        db_mod.init_schema(mock_conn)
        assert mock_cur.execute.called
        assert mock_conn.commit.called

    def test_upsert_user(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = (
            "uuid-1",
            "google-123",
            "test@example.com",
            "Test User",
            "https://pic",
            "2024-01-01",
            "2024-01-02",
        )
        user = db_mod.upsert_user(
            mock_conn, google_id="google-123", email="test@example.com", name="Test User"
        )
        assert user["id"] == "uuid-1"
        assert user["google_id"] == "google-123"
        assert mock_cur.execute.called
        assert mock_conn.commit.called

    def test_get_user_by_google_id_not_found(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = None
        assert db_mod.get_user_by_google_id(mock_conn, "nope") is None

    def test_get_user_by_google_id_found(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = (
            "uuid-1",
            "gid",
            "e@x.com",
            "Name",
            None,
            "t1",
            "t2",
        )
        user = db_mod.get_user_by_google_id(mock_conn, "gid")
        assert user["email"] == "e@x.com"

    def test_get_or_create_session(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = ("sess-1", "user-1", "t1", "t2", None, 0, 0)
        sess = db_mod.get_or_create_session(mock_conn, "user-1")
        assert sess["id"] == "sess-1"
        assert sess["user_id"] == "user-1"

    def test_load_messages_empty(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchall.return_value = []
        msgs = db_mod.load_messages(mock_conn, "sess-1")
        assert msgs == []

    def test_load_messages_with_various_roles(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchall.return_value = [
            ("system", "sys", None, None),
            ("user", "hello", None, None),
            (
                "assistant",
                "hi",
                [{"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}],
                None,
            ),
            ("tool", "result", None, "c1"),
        ]
        msgs = db_mod.load_messages(mock_conn, "sess-1")
        assert len(msgs) == 4
        assert msgs[0]["role"] == "system"
        assert msgs[2]["tool_calls"][0]["id"] == "c1"
        assert msgs[3]["tool_call_id"] == "c1"

    def test_load_messages_tool_calls_json_string(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        # When JSONB comes back as string (rare)
        mock_cur.fetchall.return_value = [
            ("assistant", "hi", '[{"id":"c1"}]', None),
        ]
        msgs = db_mod.load_messages(mock_conn, "sess-1")
        assert msgs[0]["tool_calls"] == [{"id": "c1"}]

    def test_append_message(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        # _next_seq does a SELECT MAX, then append does INSERT + UPDATE
        # Mock fetchone for _next_seq (0) and for INSERT RETURNING
        mock_cur.fetchone.side_effect = [
            (0,),  # _next_seq
            (
                "msg-id",
                "sess-1",
                0,
                "user",
                "hello",
                None,
                None,
                "2024-01-01",
                None,
                None,
                None,
                None,
            ),
        ]
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        row = db_mod.append_message(mock_conn, "sess-1", {"role": "user", "content": "hello"})
        assert row["id"] == "msg-id"
        assert row["seq"] == 0
        assert mock_conn.commit.called

    def test_append_message_with_tool_calls(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.side_effect = [
            (1,),
            (
                "msg-id2",
                "sess-1",
                1,
                "assistant",
                "",
                [{"id": "c1"}],
                None,
                "2024-01-01",
                "hy3-free",
                10,
                20,
                30,
            ),
        ]
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        row = db_mod.append_message(
            mock_conn,
            "sess-1",
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            model="hy3-free",
            total_tokens=30,
        )
        assert row["role"] == "assistant"
        # Ensure tool_calls was json-dumped for the INSERT
        insert_call = mock_cur.execute.call_args_list[1]
        args = insert_call[0][1]
        # args[4] is tool_calls_json
        assert '"c1"' in args[4]

    def test_clear_messages(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.rowcount = 5
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        n = db_mod.clear_messages(mock_conn, "sess-1")
        assert n == 5
        assert mock_conn.commit.called


# ------------------------------------------------------------------ auth
class TestAuthFile:
    def test_get_current_auth_none_when_missing(self, tmp_path, monkeypatch):
        # Point AUTH_FILE to tmp
        monkeypatch.setattr(auth_mod, "AUTH_FILE", tmp_path / "auth.json")
        monkeypatch.setattr(auth_mod, "CONFIG_DIR", tmp_path)
        assert auth_mod.get_current_auth() is None
        assert auth_mod.is_logged_in() is False

    def test_write_and_read_auth(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auth_mod, "AUTH_FILE", tmp_path / "auth.json")
        monkeypatch.setattr(auth_mod, "CONFIG_DIR", tmp_path)
        data = {
            "user_id": "u1",
            "google_id": "g1",
            "email": "a@b.com",
            "name": "A",
            "picture": None,
            "session_id": "s1",
            "last_login": "now",
        }
        auth_mod._write_auth_file(data)
        assert (tmp_path / "auth.json").exists()
        loaded = auth_mod.get_current_auth()
        assert loaded["email"] == "a@b.com"
        assert auth_mod.is_logged_in() is True

    def test_logout_clears_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auth_mod, "AUTH_FILE", tmp_path / "auth.json")
        monkeypatch.setattr(auth_mod, "CONFIG_DIR", tmp_path)
        (tmp_path / "auth.json").write_text(json.dumps({"user_id": "u1"}))
        assert auth_mod.logout_local() is True
        assert not (tmp_path / "auth.json").exists()
        assert auth_mod.logout_local() is False

    def test_validate_oauth_config_missing(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        errs = auth_mod.validate_oauth_config()
        assert any("GOOGLE_CLIENT_ID" in e for e in errs)
        assert any("GOOGLE_CLIENT_SECRET" in e for e in errs)

    def test_validate_oauth_config_ok(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csec")
        errs = auth_mod.validate_oauth_config()
        # Should not contain GOOGLE_ errors (may contain driver error if missing, but driver is installed)
        assert not any("GOOGLE_" in e for e in errs)

    def test_google_client_config(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csec")
        cfg = auth_mod._google_client_config()
        assert cfg["installed"]["client_id"] == "cid"
        assert "auth_uri" in cfg["installed"]

    def test_google_client_config_none_when_missing(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        assert auth_mod._google_client_config() is None

    def test_fetch_google_userinfo_v3(self):
        mock_creds = MagicMock()
        mock_creds.token = "tok123"
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"sub": "gid123", "email": "e@x.com", "name": "N"}
        with patch("requests.get", return_value=fake_resp) as mock_get:
            info = auth_mod.fetch_google_userinfo(mock_creds)
            assert info["sub"] == "gid123"
            assert info["email"] == "e@x.com"
            mock_get.assert_called_once()

    def test_fetch_google_userinfo_fallback_v2(self):
        mock_creds = MagicMock()
        mock_creds.token = "tok"
        # First call fails, second succeeds with "id" field
        r1 = MagicMock(status_code=401)
        r2 = MagicMock(status_code=200)
        r2.json.return_value = {"id": "gid2", "email": "e2@x.com"}
        with patch("requests.get", side_effect=[r1, r2]):
            info = auth_mod.fetch_google_userinfo(mock_creds)
            assert info["sub"] == "gid2"

    def test_login_with_browser_missing_config(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        with pytest.raises(RuntimeError, match="Missing GOOGLE"):
            auth_mod.login_with_browser(open_browser=False)


# ------------------------------------------------------------------ session
class TestSessionStoreHelpers:
    def test_is_persistence_disabled_when_no_auth(self):
        with patch("nally.auth.get_current_auth", return_value=None):
            assert sess_mod.is_persistence_enabled() is False

    def test_is_persistence_disabled_when_no_db(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auth_mod, "AUTH_FILE", tmp_path / "auth.json")
        monkeypatch.setattr(auth_mod, "CONFIG_DIR", tmp_path)
        auth_mod._write_auth_file(
            {"user_id": "u1", "google_id": "g1", "email": "a@b.com", "session_id": "s1"}
        )
        with (
            patch("nally.auth.get_current_auth", return_value={"user_id": "u1"}),
            patch("nally.db.is_configured", return_value=False),
        ):
            assert sess_mod.is_persistence_enabled() is False
        # cleanup
        (tmp_path / "auth.json").unlink(missing_ok=True)

    def test_session_store_load_append_clear(self):
        store = sess_mod.SessionStore(session_id="sess-123")
        assert store.session_id == "sess-123"

        # Mock db layer
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchall.return_value = [("user", "hi", None, None)]

        with patch("nally.db.connect", return_value=mock_conn):
            msgs = store.load()
            assert msgs == [{"role": "user", "content": "hi"}]

        # Append
        mock_conn2 = MagicMock()
        mock_cur2 = MagicMock()
        mock_cur2.fetchone.side_effect = [
            (0,),
            ("mid", "sess-123", 0, "user", "hi", None, None, "now", None, None, None, None),
        ]
        mock_conn2.cursor.return_value.__enter__.return_value = mock_cur2
        with patch("nally.db.connect", return_value=mock_conn2):
            ok = store.append({"role": "user", "content": "hi"})
            assert ok is True

        # Clear without keep_system_prompt (simple delete)
        mock_conn3 = MagicMock()
        mock_cur3 = MagicMock()
        mock_cur3.rowcount = 2
        mock_conn3.cursor.return_value.__enter__.return_value = mock_cur3
        with patch("nally.db.connect", return_value=mock_conn3):
            ok = store.clear()
            assert ok is True

        # Clear with keep_system_prompt — needs extra INSERT for system prompt
        mock_conn4 = MagicMock()
        mock_cur4 = MagicMock()
        mock_cur4.rowcount = 2
        # For append_message inside clear: _next_seq returns 0, INSERT returns row
        mock_cur4.fetchone.side_effect = [
            (0,),
            ("sys-id", "sess-123", 0, "system", "sys", None, None, "now", None, None, None, None),
        ]
        mock_conn4.cursor.return_value.__enter__.return_value = mock_cur4
        with patch("nally.db.connect", return_value=mock_conn4):
            ok = store.clear(keep_system_prompt="sys")
            assert ok is True

    def test_session_store_handles_db_error_gracefully(self):
        store = sess_mod.SessionStore(session_id="sess-err")
        with patch("nally.db.connect", side_effect=RuntimeError("db down")):
            assert store.load() == []
            assert store.append({"role": "user", "content": "hi"}) is False
            assert store.clear() is False
            assert store.count() == 0

    def test_get_session_store_none_when_not_logged_in(self):
        with patch("nally.auth.get_current_auth", return_value=None):
            assert sess_mod.get_session_store() is None

    def test_get_session_store_none_when_db_not_configured(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auth_mod, "AUTH_FILE", tmp_path / "auth.json")
        monkeypatch.setattr(auth_mod, "CONFIG_DIR", tmp_path)
        auth_mod._write_auth_file(
            {"user_id": "u1", "google_id": "g1", "email": "a@b.com", "session_id": "s1"}
        )
        with patch("nally.db.is_configured", return_value=False):
            assert sess_mod.get_session_store() is None
        (tmp_path / "auth.json").unlink(missing_ok=True)


# ------------------------------------------------------------------ Agent persistence
class FakeUsage:
    def __init__(self, prompt=5, completion=10, total=15):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = total


class FakeResponse:
    def __init__(self, content, usage=None, model="hy3-free", tool_calls=None):
        self.model = model
        self.usage = usage or FakeUsage()
        msg = MagicMock()
        msg.content = content
        msg.tool_calls = tool_calls
        self.choices = [MagicMock(message=msg)]


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.model = "hy3-free"

    def chat(self, messages, tools=None, **kwargs):
        if not self.responses:
            raise RuntimeError("no more fake responses")
        return self.responses.pop(0)


class TestAgentPersistence:
    def test_agent_runs_without_persistence_by_default(self):
        # No auth, no DB -> should still work
        llm = FakeLLM([FakeResponse("hello")])
        agent = Agent(llm_client=llm, auto_persist=True, session_store=None)
        # Since no auth file and no DB, session_store will be None
        assert agent.session_store is None
        reply = agent.run("hi")
        assert reply == "hello"
        assert len(agent.messages) == 3  # system + user + assistant

    def test_agent_persists_messages_with_fake_store(self):
        # Provide a fake store that records appends
        appended = []

        class FakeStore:
            session_id = "sess-fake"

            def load(self):
                return []

            def append(self, msg, **kwargs):
                appended.append((msg, kwargs))
                return True

            def clear(self, keep_system_prompt=None):
                return True

            def count(self):
                return len(appended)

        llm = FakeLLM([FakeResponse("done")])
        agent = Agent(llm_client=llm, session_store=FakeStore(), auto_persist=False)
        # Should have persisted system prompt on init
        assert len(appended) == 1
        assert appended[0][0]["role"] == "system"

        reply = agent.run("hello")
        assert reply == "done"
        # user + assistant should have been persisted (+ system already)
        roles = [m[0]["role"] for m in appended]
        assert roles == ["system", "user", "assistant"]
        assert appended[1][0]["content"] == "hello"
        assert appended[2][0]["content"] == "done"
        # Assistant should have model and tokens
        assert appended[2][1]["model"] == "hy3-free"
        assert appended[2][1]["total_tokens"] == 15

    def test_agent_persists_tool_calls_and_results(self):
        appended = []

        class FakeStore:
            session_id = "sess-tools"

            def load(self):
                return []

            def append(self, msg, **kwargs):
                appended.append(msg)
                return True

            def clear(self, keep_system_prompt=None):
                return True

        # LLM will request a tool then answer
        tool_tc = MagicMock()
        tool_tc.id = "call_123"
        tool_tc.function.name = "list_dir"
        tool_tc.function.arguments = '{"path": "."}'
        llm = FakeLLM(
            [
                FakeResponse("", tool_calls=[tool_tc]),
                FakeResponse("listed"),
            ]
        )
        agent = Agent(llm_client=llm, session_store=FakeStore(), auto_persist=False)
        appended.clear()  # clear system prompt
        reply = agent.run("list")
        assert reply == "listed"
        # Should have persisted: user, assistant+tool_calls, tool result, final assistant
        roles = [m["role"] for m in appended]
        assert roles == ["user", "assistant", "tool", "assistant"]
        assert appended[1]["tool_calls"][0]["id"] == "call_123"
        assert appended[2]["tool_call_id"] == "call_123"

    def test_agent_loads_existing_history(self):
        persisted = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "old hi"},
            {"role": "assistant", "content": "old hello"},
        ]

        class FakeStore:
            session_id = "sess-load"

            def load(self):
                return list(persisted)

            def append(self, *a, **kw):
                return True

            def clear(self, **kw):
                return True

        llm = FakeLLM([FakeResponse("new")])
        agent = Agent(llm_client=llm, session_store=FakeStore(), auto_persist=False)
        assert agent.messages == persisted  # loaded from store, not fresh system prompt
        reply = agent.run("new msg")
        assert reply == "new"
        assert len(agent.messages) == 5  # persisted 3 + user + assistant

    def test_agent_clear_clears_store(self):
        cleared = {}

        class FakeStore:
            session_id = "sess-clear"

            def load(self):
                return []

            def append(self, *a, **kw):
                return True

            def clear(self, keep_system_prompt=None):
                cleared["prompt"] = keep_system_prompt
                return True

        llm = FakeLLM([FakeResponse("hi")])
        agent = Agent(llm_client=llm, session_store=FakeStore(), auto_persist=False)
        agent.run("hello")
        agent.clear_history()
        assert cleared["prompt"] == agent.messages[0]["content"]
        assert len(agent.messages) == 1

    def test_agent_no_persist_flag(self):
        # Ensure --no-persist disables even if store would be auto-discovered
        with patch("nally.session.get_session_store", return_value=MagicMock()):
            llm = FakeLLM([FakeResponse("hi")])
            agent = Agent(llm_client=llm, auto_persist=False, session_store=None)
            assert agent.session_store is None

    def test_agent_handles_store_error_gracefully(self):
        class BadStore:
            session_id = "bad"

            def load(self):
                raise RuntimeError("db down")

            def append(self, *a, **kw):
                raise RuntimeError("db down")

            def clear(self, **kw):
                raise RuntimeError("db down")

        llm = FakeLLM([FakeResponse("ok")])
        # Should not crash on init even if load fails
        agent = Agent(llm_client=llm, session_store=BadStore(), auto_persist=False)
        assert agent.messages[0]["role"] == "system"
        # Run should still succeed even if append fails
        reply = agent.run("hi")
        assert reply == "ok"


# ------------------------------------------------------------------ main dispatch
class TestMainDispatch:
    def test_chat_parser_hello(self, monkeypatch):
        from main import build_chat_parser

        p = build_chat_parser()
        args = p.parse_args(["hello", "world"])
        assert args.prompt == ["hello", "world"]
        assert args.no_persist is False

    def test_main_history_no_session(self, monkeypatch, capsys):
        from main import main

        # Ensure no auth
        with (
            patch("nally.auth.get_current_auth", return_value=None),
            patch("nally.db.is_configured", return_value=False),
        ):
            rc = main(["history"])
            assert rc == 0
            out = capsys.readouterr().out
            assert "No persisted session" in out

    def test_main_auth_status(self, monkeypatch, capsys):
        from main import main

        with (
            patch("nally.auth.get_current_auth", return_value=None),
            patch("nally.db.is_configured", return_value=False),
            patch("nally.session.get_session_store", return_value=None),
        ):
            rc = main(["auth", "status"])
            assert rc == 0
            out = capsys.readouterr().out
            assert "logged_in: False" in out

    def test_main_requires_api_key_for_chat(self, monkeypatch, capsys):
        from main import main

        # main imports validate_config directly, so patch where it's used
        with patch("main.validate_config", return_value=["Missing API key"]):
            rc = main(["hello"])
            assert rc == 2
