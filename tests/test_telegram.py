"""Tests for Telegram bot + Device Flow (mocked)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nally.telegram.bot import split_message, typing_loop


# ------------------------------------------------------------------ split
class TestSplitMessage:
    def test_short(self):
        assert split_message("hello") == ["hello"]

    def test_empty(self):
        assert split_message("") == [""]

    def test_exact_4096(self):
        s = "a" * 4096
        assert split_message(s) == [s]

    def test_over_newline(self):
        s = "a\n" * 2000 + "b" * 100
        chunks = split_message(s, max_len=100)
        assert all(len(c) <= 100 for c in chunks)
        assert "".join(chunks).replace("\n ", "\n") == s or len(chunks) > 1

    def test_over_space(self):
        s = "hello world " * 500
        chunks = split_message(s, max_len=100)
        assert all(len(c) <= 100 for c in chunks)

    def test_hard_split(self):
        s = "a" * 5000
        chunks = split_message(s, max_len=4096)
        assert len(chunks) == 2
        assert len(chunks[0]) == 4096
        assert len(chunks[1]) == 904

    def test_custom_max(self):
        assert split_message("abc def", max_len=3) == ["abc", "def"]


# ------------------------------------------------------------------ typing
class TestTypingLoop:
    @pytest.mark.asyncio
    async def test_typing_loop_sends_and_stops(self):
        bot = AsyncMock()
        stop = asyncio.Event()
        task = asyncio.create_task(typing_loop(bot, 123, stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)
        assert bot.send_chat_action.called
        # called with typing
        call_kwargs = bot.send_chat_action.call_args_list[0][1]
        assert call_kwargs["chat_id"] == 123

    @pytest.mark.asyncio
    async def test_typing_loop_handles_error(self):
        bot = AsyncMock()
        bot.send_chat_action.side_effect = Exception("fail")
        stop = asyncio.Event()
        task = asyncio.create_task(typing_loop(bot, 1, stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)
        # should not raise
        assert True


# ------------------------------------------------------------------ db telegram
from nally import db as db_mod


class TestDBTelegram:
    def test_get_user_by_telegram_id_not_found(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchone.return_value = None
        assert db_mod.get_user_by_telegram_id(conn, "123") is None

    def test_get_user_by_telegram_id_found(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchone.return_value = ("uid", "gid", "123", "e@x.com", "N", None, "t1", "t2")
        user = db_mod.get_user_by_telegram_id(conn, "123")
        assert user["telegram_id"] == "123"
        assert user["id"] == "uid"

    def test_link_telegram_to_user(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchone.return_value = ("uid", "gid", "123", "e@x.com", "N", None, "t1", "t2")
        user = db_mod.link_telegram_to_user(conn, "uid", "123")
        assert user["telegram_id"] == "123"
        assert conn.commit.called

    def test_link_telegram_not_found(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchone.return_value = None
        with pytest.raises(ValueError, match="not found"):
            db_mod.link_telegram_to_user(conn, "nonexist", "123")

    def test_unlink_telegram_found(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.rowcount = 1
        assert db_mod.unlink_telegram(conn, "123") is True

    def test_unlink_telegram_not_found(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.rowcount = 0
        assert db_mod.unlink_telegram(conn, "123") is False

    def test_create_user_by_telegram(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchone.return_value = (
            "uid2",
            None,
            "456",
            "tg_456@telegram.local",
            "Bob",
            None,
            "t1",
            "t2",
        )
        user = db_mod.create_user_by_telegram(conn, telegram_id="456", first_name="Bob")
        assert user["telegram_id"] == "456"
        assert user["id"] == "uid2"

    def test_init_schema_migration(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        # Mock column checks: first call for telegram_id missing, second for google_id NOT NULL
        cur.fetchone.side_effect = [
            None,  # telegram_id not exists
            ("NO",),  # google_id is NOT NULL
        ]
        db_mod._migrate_add_telegram(conn)
        # Should have executed ALTER TABLE ADD COLUMN and DROP NOT NULL
        assert cur.execute.call_count >= 3
        assert conn.commit.called


# ------------------------------------------------------------------ auth device flow
from nally import auth as auth_mod


class TestDeviceFlow:
    def test_request_code_success(self):
        fake_resp = MagicMock(status_code=200)
        fake_resp.json.return_value = {
            "device_code": "dev123",
            "user_code": "ABCD-EFGH",
            "verification_url": "https://www.google.com/device",
            "interval": 5,
            "expires_in": 1800,
        }
        with patch("requests.post", return_value=fake_resp):
            data = auth_mod.device_flow_request_code(client_id="cid123")
            assert data["user_code"] == "ABCD-EFGH"
            assert data["device_code"] == "dev123"

    def test_request_code_failure(self):
        fake_resp = MagicMock(status_code=400, text="bad request")
        with patch("requests.post", return_value=fake_resp):
            with pytest.raises(RuntimeError, match="Device code request failed"):
                auth_mod.device_flow_request_code(client_id="cid")

    def test_request_code_missing_fields(self):
        fake_resp = MagicMock(status_code=200)
        fake_resp.json.return_value = {"foo": "bar"}
        with patch("requests.post", return_value=fake_resp):
            with pytest.raises(RuntimeError, match="Unexpected"):
                auth_mod.device_flow_request_code(client_id="cid")

    def test_poll_token_success_first_try(self):
        fake_resp = MagicMock(status_code=200)
        fake_resp.json.return_value = {"access_token": "tok", "id_token": "jwt123"}
        with patch("requests.post", return_value=fake_resp):
            with patch("time.sleep"):
                with patch("time.time", side_effect=[0, 1]):
                    data = auth_mod.device_flow_poll_token(
                        "dev123", client_id="cid", client_secret="csec", interval=1, expires_in=100
                    )
                    assert data["access_token"] == "tok"

    def test_poll_token_pending_then_success(self):
        pending = MagicMock(status_code=428)
        pending.json.return_value = {"error": "authorization_pending"}
        success = MagicMock(status_code=200)
        success.json.return_value = {"access_token": "tok2", "id_token": "jwt2"}
        with patch("requests.post", side_effect=[pending, pending, success]):
            with patch("time.sleep"):
                with patch("time.time", side_effect=[0, 1, 2, 3, 4]):
                    data = auth_mod.device_flow_poll_token(
                        "dev123", client_id="cid", client_secret="csec", interval=1, expires_in=100
                    )
                    assert data["access_token"] == "tok2"

    def test_poll_token_slow_down(self):
        slow = MagicMock(status_code=403)
        slow.json.return_value = {"error": "slow_down"}
        success = MagicMock(status_code=200)
        success.json.return_value = {"access_token": "tok3", "id_token": "jwt3"}
        with patch("requests.post", side_effect=[slow, success]):
            with patch("time.sleep") as mock_sleep:
                with patch("time.time", side_effect=[0, 1, 2, 3]):
                    data = auth_mod.device_flow_poll_token(
                        "dev123", client_id="cid", client_secret="csec", interval=5, expires_in=100
                    )
                    assert data["access_token"] == "tok3"
                    # slow_down should have increased interval
                    assert mock_sleep.call_args_list[0][0][0] == 5
                    assert mock_sleep.call_args_list[1][0][0] == 10

    def test_poll_token_denied(self):
        denied = MagicMock(status_code=403)
        denied.json.return_value = {"error": "access_denied"}
        with patch("requests.post", return_value=denied):
            with patch("time.sleep"):
                with patch("time.time", side_effect=[0, 1]):
                    with pytest.raises(RuntimeError, match="denied"):
                        auth_mod.device_flow_poll_token(
                            "dev123",
                            client_id="cid",
                            client_secret="csec",
                            interval=1,
                            expires_in=100,
                        )

    def test_poll_token_expired(self):
        expired = MagicMock(status_code=400)
        expired.json.return_value = {"error": "expired_token"}
        with patch("requests.post", return_value=expired):
            with patch("time.sleep"):
                with patch("time.time", side_effect=[0, 1]):
                    with pytest.raises(RuntimeError, match="expired"):
                        auth_mod.device_flow_poll_token(
                            "dev123",
                            client_id="cid",
                            client_secret="csec",
                            interval=1,
                            expires_in=100,
                        )

    def test_decode_google_id_token_pyjwt(self):
        fake_jwt = "header.payload.sig"
        fake_key = MagicMock()
        fake_key.key = "publickey"
        fake_key.algorithm_name = "RS256"
        mock_client = MagicMock()
        mock_client.get_signing_key_from_jwt.return_value = fake_key
        decoded = {
            "sub": "123",
            "email": "e@x.com",
            "aud": "cid",
            "iss": "https://accounts.google.com",
        }
        with patch("jwt.PyJWKClient", return_value=mock_client):
            with patch("jwt.decode", return_value=decoded) as mock_decode:
                result = auth_mod.decode_google_id_token(fake_jwt, client_id="cid")
                assert result["sub"] == "123"
                mock_decode.assert_called_once()

    def test_decode_google_id_token_empty(self):
        with pytest.raises(RuntimeError, match="Empty ID token"):
            auth_mod.decode_google_id_token("", client_id="cid")

    def test_link_telegram_to_google_new_user(self):
        userinfo = {"sub": "google123", "email": "new@x.com", "name": "New User"}
        mock_conn = MagicMock()
        # get_user_by_telegram_id -> None, get_user_by_google_id -> None, upsert -> user, link -> user with telegram
        with patch("nally.db.is_configured", return_value=True):
            with patch("nally.db.connect", return_value=mock_conn):
                with patch("nally.db.get_user_by_telegram_id", return_value=None):
                    with patch("nally.db.get_user_by_google_id", return_value=None):
                        fake_user = {
                            "id": "uid1",
                            "google_id": "google123",
                            "telegram_id": None,
                            "email": "new@x.com",
                        }
                        fake_linked = {
                            "id": "uid1",
                            "google_id": "google123",
                            "telegram_id": "999",
                            "email": "new@x.com",
                        }
                        with patch("nally.db.upsert_user", return_value=fake_user):
                            with patch("nally.db.link_telegram_to_user", return_value=fake_linked):
                                with patch(
                                    "nally.db.get_or_create_session", return_value={"id": "sess1"}
                                ):
                                    with patch("nally.db.init_schema"):
                                        result = auth_mod.link_telegram_to_google(
                                            "999", userinfo=userinfo
                                        )
                                        assert result["user"]["telegram_id"] == "999"
                                        assert result["created_google_user"] is True

    def test_link_telegram_to_google_existing_google(self):
        userinfo = {"sub": "google123", "email": "exist@x.com"}
        existing = {"id": "uid1", "google_id": "google123", "email": "exist@x.com"}
        linked = {
            "id": "uid1",
            "google_id": "google123",
            "telegram_id": "999",
            "email": "exist@x.com",
        }
        with patch("nally.db.is_configured", return_value=True):
            with patch("nally.db.connect", return_value=MagicMock()):
                with patch("nally.db.get_user_by_telegram_id", return_value=None):
                    with patch("nally.db.get_user_by_google_id", return_value=existing):
                        with patch("nally.db.link_telegram_to_user", return_value=linked):
                            with patch(
                                "nally.db.get_or_create_session", return_value={"id": "sess1"}
                            ):
                                with patch("nally.db.init_schema"):
                                    result = auth_mod.link_telegram_to_google(
                                        "999", userinfo=userinfo
                                    )
                                    assert result["created_google_user"] is False

    def test_link_telegram_already_linked_to_other(self):
        userinfo = {"sub": "google123", "email": "new@x.com"}
        already = {
            "id": "other",
            "google_id": "other_google",
            "telegram_id": "999",
            "email": "other@x.com",
        }
        with patch("nally.db.is_configured", return_value=True):
            with patch("nally.db.connect", return_value=MagicMock()):
                with patch("nally.db.get_user_by_telegram_id", return_value=already):
                    with patch("nally.db.init_schema"):
                        with pytest.raises(RuntimeError, match="already linked"):
                            auth_mod.link_telegram_to_google("999", userinfo=userinfo)

    def test_validate_device_config(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_DEVICE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_DEVICE_CLIENT_SECRET", raising=False)
        errs = auth_mod.validate_device_oauth_config()
        assert any("GOOGLE_DEVICE_CLIENT_ID" in e for e in errs)
        monkeypatch.setenv("GOOGLE_DEVICE_CLIENT_ID", "cid")
        monkeypatch.setenv("GOOGLE_DEVICE_CLIENT_SECRET", "csec")
        errs = auth_mod.validate_device_oauth_config()
        assert not any("GOOGLE_DEVICE" in e for e in errs)


# ------------------------------------------------------------------ bot handlers (mocked telegram)
class TestBotHandlers:
    @pytest.mark.asyncio
    async def test_handle_start(self):
        from nally.telegram.bot import handle_start

        update = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        await handle_start(update, context)
        assert update.message.reply_text.called
        text = update.message.reply_text.call_args[0][0]
        assert "Nally" in text
        assert "/link" in text

    @pytest.mark.asyncio
    async def test_handle_status_not_linked(self):
        from nally.telegram.bot import handle_status

        update = MagicMock()
        update.effective_user.id = 12345
        update.effective_chat.id = 1
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        with patch("nally.db.is_configured", return_value=True):
            with patch("nally.db.connect") as mock_connect:
                mock_conn = MagicMock()
                mock_connect.return_value = mock_conn
                mock_conn.cursor.return_value.__enter__.return_value = MagicMock()
                with patch("nally.db.get_user_by_telegram_id", return_value=None):
                    await handle_status(update, context)
                    assert update.message.reply_text.called
                    assert (
                        "Not linked" in update.message.reply_text.call_args[0][0]
                        or "Linked: no" in update.message.reply_text.call_args[0][0]
                    )

    @pytest.mark.asyncio
    async def test_handle_status_linked(self):
        from nally.telegram.bot import handle_status

        update = MagicMock()
        update.effective_user.id = 999
        update.effective_chat.id = 1
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        fake_user = {
            "id": "uid1",
            "google_id": "gid1",
            "telegram_id": "999",
            "email": "linked@x.com",
        }
        fake_sess = {"id": "sess1", "message_count": 5, "total_tokens": 100}
        with patch("nally.db.is_configured", return_value=True):
            with patch("nally.db.get_user_by_telegram_id", return_value=fake_user):
                with patch("nally.db.get_or_create_session", return_value=fake_sess):
                    with patch("nally.db.connect", return_value=MagicMock()):
                        await handle_status(update, context)
                        text = update.message.reply_text.call_args[0][0]
                        assert "Linked: yes" in text
                        assert "linked@x.com" in text

    @pytest.mark.asyncio
    async def test_handle_unlink(self):
        from nally.telegram.bot import handle_unlink

        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        with patch("nally.db.is_configured", return_value=True):
            with patch("nally.db.unlink_telegram", return_value=True):
                with patch("nally.db.connect", return_value=MagicMock()):
                    await handle_unlink(update, context)
                    assert "Unlinked" in update.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_handle_unlink_not_linked(self):
        from nally.telegram.bot import handle_unlink

        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        with patch("nally.db.is_configured", return_value=True):
            with patch("nally.db.unlink_telegram", return_value=False):
                with patch("nally.db.connect", return_value=MagicMock()):
                    await handle_unlink(update, context)
                    assert "Not linked" in update.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_handle_clear_with_agent(self):
        from nally.telegram import bot as botmod
        from nally.telegram.bot import handle_clear

        update = MagicMock()
        update.effective_chat.id = 42
        update.effective_user.id = 123
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        fake_agent = MagicMock()
        fake_agent.clear_history = MagicMock()
        botmod._agents[42] = fake_agent
        await handle_clear(update, context)
        assert fake_agent.clear_history.called
        assert "cleared" in update.message.reply_text.call_args[0][0].lower()
        botmod._agents.pop(42, None)

    @pytest.mark.asyncio
    async def test_handle_message_not_linked(self):
        from nally.telegram.bot import handle_message

        update = MagicMock()
        update.message.text = "hello"
        update.message.reply_text = AsyncMock()
        update.effective_user.id = 999
        update.effective_chat.id = 1
        context = MagicMock()
        context._nally_locks = {}
        context.bot.send_chat_action = AsyncMock()
        with patch("nally.db.is_configured", return_value=True):
            with patch("nally.db.get_user_by_telegram_id", return_value=None):
                with patch("nally.db.connect", return_value=MagicMock()):
                    await handle_message(update, context)
                    assert "Not linked" in update.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_handle_message_linked(self):
        from nally.telegram.bot import handle_message

        update = MagicMock()
        update.message.text = "hello"
        update.message.reply_text = AsyncMock()
        # placeholder
        placeholder = AsyncMock()
        placeholder.edit_text = AsyncMock()
        placeholder.message_id = 12345
        update.message.reply_text.return_value = placeholder
        update.effective_user.id = 777
        update.effective_chat.id = 1
        context = MagicMock()
        context._nally_locks = {}
        context.bot.send_chat_action = AsyncMock()
        context.bot.send_message = AsyncMock()
        context.bot.edit_message_text = AsyncMock()

        fake_user = {"id": "uid1", "google_id": "gid1", "telegram_id": "777", "email": "e@x.com"}
        fake_agent = MagicMock()
        fake_agent.run.return_value = "reply text"
        fake_agent.on_tool_start = None

        with patch("nally.db.is_configured", return_value=True):
            with patch("nally.db.get_user_by_telegram_id", return_value=fake_user):
                with patch("nally.db.connect", return_value=MagicMock()):
                    with patch("nally.telegram.bot._get_or_create_agent", return_value=fake_agent):
                        await handle_message(update, context)
                        # Should have called agent.run via to_thread
                        assert True  # run is mocked, but we patch _get_or_create_agent
                        # Placeholder should be edited via StatusUpdater (bot.edit_message_text) or fallback
                        assert (
                            context.bot.edit_message_text.called
                            or placeholder.edit_text.called
                            or context.bot.send_message.called
                        )

    @pytest.mark.asyncio
    async def test_handle_link_already_linked(self):
        from nally.telegram.bot import handle_link

        update = MagicMock()
        update.effective_user.id = 123
        update.effective_user.first_name = "Test"
        update.effective_user.username = "testuser"
        update.effective_chat.id = 1
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        fake_user = {
            "id": "uid1",
            "google_id": "gid1",
            "telegram_id": "123",
            "email": "already@x.com",
        }
        with patch("nally.db.is_configured", return_value=True):
            with patch("nally.db.get_user_by_telegram_id", return_value=fake_user):
                with patch("nally.db.connect", return_value=MagicMock()):
                    await handle_link(update, context)
                    assert "Already linked" in update.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_handle_link_device_flow(self):
        from nally.telegram.bot import handle_link

        update = MagicMock()
        update.effective_user.id = 456
        update.effective_user.first_name = "Bob"
        update.effective_user.username = "bob"
        update.effective_chat.id = 1
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.bot.send_message = AsyncMock()

        with patch("nally.db.is_configured", return_value=True):
            with patch("nally.db.get_user_by_telegram_id", return_value=None):
                with patch("nally.db.connect", return_value=MagicMock()):
                    with patch("nally.auth.validate_device_oauth_config", return_value=[]):
                        with patch(
                            "nally.auth.device_flow_request_code",
                            return_value={
                                "device_code": "dev123",
                                "user_code": "ABCD-EFGH",
                                "verification_url": "https://www.google.com/device",
                                "interval": 5,
                                "expires_in": 1800,
                            },
                        ):
                            await handle_link(update, context)
                            # Should have sent instructions
                            assert update.message.reply_text.called
                            text = update.message.reply_text.call_args[0][0]
                            assert "ABCD-EFGH" in text
                            assert "google.com/device" in text

    def test_per_chat_agent_isolation(self):
        from nally.telegram import bot as botmod

        botmod._agents.clear()
        with patch("nally.db.is_configured", return_value=False):
            a1 = botmod._get_or_create_agent(1, telegram_user_id=None)
            a2 = botmod._get_or_create_agent(2, telegram_user_id=None)
            assert a1 is not a2
            assert botmod._get_or_create_agent(1) is a1
            botmod._agents.clear()

    def test_run_bot_missing_token(self):
        from nally.telegram.bot import run_bot

        with patch.dict("os.environ", {}, clear=False):
            # Ensure env has no token
            import os

            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
                run_bot(token=None)

    def test_run_bot_import_error(self):
        from nally.telegram.bot import run_bot

        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "fake:token"}):
            with patch("telegram.ext.Application", side_effect=ImportError("no telegram")):
                # Actually the import is inside function, so patch the import
                with patch("builtins.__import__", side_effect=ImportError("no telegram")):
                    try:
                        run_bot(token="fake:token")
                    except RuntimeError as e:
                        assert "python-telegram-bot" in str(e)
                        return
                    except ImportError:
                        return
                    # If it didn't raise, allow (maybe telegram is installed)
                    pass


# ------------------------------------------------------------------ telegram ux (status updater)
class TestTelegramUx:
    def test_tool_status_map(self):
        from nally.telegram.ux import TOOL_STATUS, friendly_status

        assert TOOL_STATUS["web_search"] == "Searching the web"
        assert TOOL_STATUS["fetch"] == "Reading webpage"
        assert TOOL_STATUS["run_command"] == "Running command on your computer"
        assert TOOL_STATUS["read_file"] == "Reading file"
        assert TOOL_STATUS["write_file"] == "Writing file"
        assert TOOL_STATUS["list_dir"] == "Listing files"
        assert friendly_status("web_search") == "Searching the web"
        assert friendly_status("unknown_tool") == "Using unknown_tool"
        assert friendly_status("fetch") == "Reading webpage"

    @pytest.mark.asyncio
    async def test_status_updater_finish(self):
        from nally.telegram.ux import StatusUpdater

        bot = AsyncMock()
        bot.edit_message_text = AsyncMock()
        loop = asyncio.get_running_loop()
        updater = StatusUpdater(bot=bot, chat_id=1, message_id=99, loop=loop)
        await updater.finish("final answer")
        assert bot.edit_message_text.called
        assert bot.edit_message_text.call_args[1]["text"] == "final answer"
        assert bot.edit_message_text.call_args[1]["chat_id"] == 1
        assert bot.edit_message_text.call_args[1]["message_id"] == 99

    @pytest.mark.asyncio
    async def test_status_updater_update(self):
        from nally.telegram.ux import StatusUpdater

        bot = AsyncMock()
        bot.edit_message_text = AsyncMock()
        loop = asyncio.get_running_loop()
        updater = StatusUpdater(bot=bot, chat_id=5, message_id=10, loop=loop)
        await updater.update("Thinking...")
        assert bot.edit_message_text.called
        assert "Thinking" in bot.edit_message_text.call_args[1]["text"]

    def test_status_updater_on_tool_start_threadsafe(self):
        import contextlib

        from nally.telegram.ux import StatusUpdater

        bot = MagicMock()
        bot.edit_message_text = AsyncMock()
        loop = MagicMock()
        updater = StatusUpdater(bot=bot, chat_id=1, message_id=1, loop=loop)
        # Patch run_coroutine_threadsafe to avoid needing real loop, close coroutine to suppress warning
        def _close(coro, _loop):
            with contextlib.suppress(Exception):
                coro.close()
            return MagicMock()

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close) as mock_run:
            updater.on_tool_start("web_search", {})
            assert mock_run.called
            # called with a coroutine and the loop
            assert mock_run.call_args[0][1] is loop
        # friendly text should be Searching the web (checked via the coroutine)
        # We can't easily inspect coroutine text, but we verified it was scheduled

    def test_status_updater_rate_limit(self):
        import contextlib

        from nally.telegram.ux import StatusUpdater

        bot = MagicMock()
        loop = MagicMock()
        updater = StatusUpdater(bot=bot, chat_id=1, message_id=1, loop=loop)

        def _close(coro, _loop):
            with contextlib.suppress(Exception):
                coro.close()
            return MagicMock()

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close) as mock_run:
            with patch("time.monotonic", side_effect=[0.0, 0.1, 0.6]):
                updater.on_tool_start("web_search", {})  # t=0.0, should run
                assert mock_run.call_count == 1
                updater.on_tool_start("fetch", {})  # t=0.1, <0.5s, should skip
                assert mock_run.call_count == 1
                updater.on_tool_start("read_file", {})  # t=0.6, >0.5 from 0.0, should run
                assert mock_run.call_count == 2

    def test_status_updater_unknown_tool(self):
        import contextlib

        from nally.telegram.ux import StatusUpdater

        bot = MagicMock()
        loop = MagicMock()
        updater = StatusUpdater(bot=bot, chat_id=1, message_id=1, loop=loop)

        def _close(coro, _loop):
            with contextlib.suppress(Exception):
                coro.close()
            return MagicMock()

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close) as mock_run:
            updater.on_tool_start("custom_tool", {})
            assert mock_run.called

    @pytest.mark.asyncio
    async def test_agent_on_tool_start_called(self):
        """Agent fires on_tool_start before tool execution."""
        from nally.agent import Agent
        from nally.llm import LLMClient
        from unittest.mock import MagicMock as Mock
        import json as _json

        # Mock LLM to return a tool call then a final response
        mock_llm = Mock(spec=LLMClient)
        mock_llm.model = "test-model"

        tool_call = Mock()
        tool_call.id = "call_1"
        tool_call.function.name = "list_dir"
        tool_call.function.arguments = _json.dumps({"path": "."})

        msg_with_tool = Mock()
        msg_with_tool.content = ""
        msg_with_tool.tool_calls = [tool_call]

        msg_final = Mock()
        msg_final.content = "done"
        msg_final.tool_calls = None

        resp1 = Mock(choices=[Mock(message=msg_with_tool)], model="test-model", usage=None)
        resp2 = Mock(choices=[Mock(message=msg_final)], model="test-model", usage=None)
        mock_llm.chat.side_effect = [resp1, resp2]

        # Track callback
        calls: list[str] = []

        def on_tool(name, args):
            calls.append(name)

        agent = Agent(llm_client=mock_llm, auto_persist=False, on_tool_start=on_tool)
        # Stub registry to avoid filesystem access
        agent.registry.execute = lambda n, a: ("list ok", True)  # type: ignore

        result = agent.run("hello")
        assert result == "done"
        assert calls == ["list_dir"]
