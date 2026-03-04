"""Tests for TelegramBridge with mocked HTTP."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from server.bridges.telegram import TelegramBridge


def _make_response(data: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=data,
        request=httpx.Request("POST", "https://api.telegram.org/test"),
    )


@pytest.fixture
def manager():
    mgr = MagicMock()
    mgr.handle_incoming = AsyncMock()
    mgr.handle_tool_decision = AsyncMock()
    return mgr


@pytest.fixture
def bridge(manager):
    b = TelegramBridge(manager, token="test-token")
    # Mock the HTTP client
    b._client = MagicMock(spec=httpx.AsyncClient)
    b._client.post = AsyncMock(
        return_value=_make_response({"ok": True, "result": {}})
    )
    return b


class TestSendText:
    async def test_short_message(self, bridge):
        await bridge.send_text("123", "hello")
        bridge._client.post.assert_called_once()
        call_data = bridge._client.post.call_args[1]["json"]
        assert call_data["chat_id"] == 123
        assert call_data["text"] == "hello"

    async def test_split_long_message(self, bridge):
        long_text = "a" * 5000
        await bridge.send_text("123", long_text)
        assert bridge._client.post.call_count == 2


class TestSendToolApproval:
    async def test_sends_inline_keyboard(self, bridge):
        await bridge.send_tool_approval_request(
            "123", "tu1", "Bash", {"command": "ls"}
        )
        call_data = bridge._client.post.call_args[1]["json"]
        assert "reply_markup" in call_data
        keyboard = call_data["reply_markup"]["inline_keyboard"]
        assert len(keyboard) == 1
        assert len(keyboard[0]) == 2
        assert keyboard[0][0]["callback_data"] == "approve:tu1"
        assert keyboard[0][1]["callback_data"] == "deny:tu1"


class TestSendToolUse:
    async def test_with_command(self, bridge):
        await bridge.send_tool_use("123", "Bash", {"command": "ls -la"})
        call_data = bridge._client.post.call_args[1]["json"]
        assert "Bash" in call_data["text"]
        assert "ls -la" in call_data["text"]

    async def test_with_file_path(self, bridge):
        await bridge.send_tool_use("123", "Read", {"file_path": "/tmp/f.txt"})
        call_data = bridge._client.post.call_args[1]["json"]
        assert "/tmp/f.txt" in call_data["text"]


class TestSendResult:
    async def test_success(self, bridge):
        await bridge.send_result("123", 0.0123, False)
        call_data = bridge._client.post.call_args[1]["json"]
        assert "Done" in call_data["text"]
        assert "$0.0123" in call_data["text"]

    async def test_error(self, bridge):
        await bridge.send_result("123", None, True)
        call_data = bridge._client.post.call_args[1]["json"]
        assert "Error" in call_data["text"]


class TestSendStatus:
    async def test_running_sends_typing(self, bridge):
        await bridge.send_status("123", "running")
        call_data = bridge._client.post.call_args[1]["json"]
        assert call_data["action"] == "typing"

    async def test_idle_no_message(self, bridge):
        bridge._client.post.reset_mock()
        await bridge.send_status("123", "idle")
        bridge._client.post.assert_not_called()


class TestHandleUpdate:
    async def test_text_message(self, bridge, manager):
        update = {
            "update_id": 1,
            "message": {
                "chat": {"id": 123},
                "text": "hello",
            },
        }
        await bridge._handle_update(update)
        manager.handle_incoming.assert_called_once_with(
            "telegram", "123", "hello", bridge
        )

    async def test_callback_query_approve(self, bridge, manager):
        query = {
            "id": "q1",
            "data": "approve:tu1",
            "message": {"chat": {"id": 123}, "message_id": 42},
        }
        await bridge._handle_callback_query(query)
        manager.handle_tool_decision.assert_called_once_with(
            "telegram", "123", "tu1", True
        )

    async def test_callback_query_deny(self, bridge, manager):
        query = {
            "id": "q1",
            "data": "deny:tu1",
            "message": {"chat": {"id": 123}, "message_id": 42},
        }
        await bridge._handle_callback_query(query)
        manager.handle_tool_decision.assert_called_once_with(
            "telegram", "123", "tu1", False
        )

    async def test_access_control_rejects(self, manager):
        b = TelegramBridge(manager, token="test", allowed_chat_ids=["999"])
        b._client = MagicMock(spec=httpx.AsyncClient)
        update = {
            "update_id": 1,
            "message": {"chat": {"id": 123}, "text": "hello"},
        }
        await b._handle_update(update)
        manager.handle_incoming.assert_not_called()

    async def test_no_text_message_ignored(self, bridge, manager):
        update = {
            "update_id": 1,
            "message": {"chat": {"id": 123}},  # no "text" field
        }
        await bridge._handle_update(update)
        manager.handle_incoming.assert_not_called()


class TestSplitText:
    def test_short_text(self):
        assert TelegramBridge._split_text("hello", 4096) == ["hello"]

    def test_long_text_splits_at_newline(self):
        text = "line1\n" * 1000
        chunks = TelegramBridge._split_text(text, 100)
        for chunk in chunks:
            assert len(chunk) <= 100

    def test_no_newline_splits_at_max(self):
        text = "a" * 200
        chunks = TelegramBridge._split_text(text, 100)
        assert chunks == ["a" * 100, "a" * 100]


class TestApiCall:
    async def test_rate_limit_retry(self, bridge):
        bridge._client.post = AsyncMock(
            side_effect=[
                _make_response(
                    {"ok": False, "parameters": {"retry_after": 0}},
                    status_code=429,
                ),
                _make_response({"ok": True, "result": {"message_id": 1}}),
            ]
        )
        result = await bridge._api_call("sendMessage", {"chat_id": 1, "text": "hi"})
        assert result is not None
        assert bridge._client.post.call_count == 2
