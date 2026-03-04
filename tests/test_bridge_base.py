"""Tests for Bridge ABC and TextBuffer."""

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from server.bridges.base import Bridge, TextBuffer


# --- Mock Bridge for testing ---


class MockBridge(Bridge):
    """Concrete bridge that records all send calls."""

    name = "mock"

    def __init__(self):
        super().__init__(manager=MagicMock())
        self.calls: list[tuple[str, dict]] = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send_text(self, chat_id: str, text: str) -> None:
        self.calls.append(("send_text", {"chat_id": chat_id, "text": text}))

    async def send_tool_approval_request(
        self,
        chat_id: str,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> None:
        self.calls.append(
            (
                "send_tool_approval_request",
                {
                    "chat_id": chat_id,
                    "tool_use_id": tool_use_id,
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                },
            )
        )

    async def send_tool_use(
        self, chat_id: str, tool_name: str, tool_input: dict[str, Any]
    ) -> None:
        self.calls.append(
            (
                "send_tool_use",
                {"chat_id": chat_id, "tool_name": tool_name, "tool_input": tool_input},
            )
        )

    async def send_tool_result(
        self, chat_id: str, output: str, is_error: bool
    ) -> None:
        self.calls.append(
            (
                "send_tool_result",
                {"chat_id": chat_id, "output": output, "is_error": is_error},
            )
        )

    async def send_status(self, chat_id: str, status: str) -> None:
        self.calls.append(("send_status", {"chat_id": chat_id, "status": status}))

    async def send_result(
        self, chat_id: str, cost: float | None, is_error: bool
    ) -> None:
        self.calls.append(
            ("send_result", {"chat_id": chat_id, "cost": cost, "is_error": is_error})
        )

    async def send_error(self, chat_id: str, message: str) -> None:
        self.calls.append(
            ("send_error", {"chat_id": chat_id, "message": message})
        )


# --- TextBuffer tests ---


class TestTextBuffer:
    async def test_flush_forces_immediate(self):
        flushed = []
        buf = TextBuffer(
            max_size=100,
            flush_delay=10.0,
            on_flush=lambda t: _async_append(flushed, t),
        )
        await buf.append("hello")
        await buf.flush()
        assert flushed == ["hello"]

    async def test_append_over_max_flushes_immediately(self):
        flushed = []
        buf = TextBuffer(
            max_size=10,
            flush_delay=10.0,
            on_flush=lambda t: _async_append(flushed, t),
        )
        await buf.append("a" * 15)
        assert len(flushed) == 1
        assert flushed[0] == "a" * 15

    async def test_append_multiple_under_max_batches(self):
        flushed = []
        buf = TextBuffer(
            max_size=100,
            flush_delay=0.05,
            on_flush=lambda t: _async_append(flushed, t),
        )
        await buf.append("hello ")
        await buf.append("world")
        await asyncio.sleep(0.1)
        assert flushed == ["hello world"]

    async def test_empty_flush_is_noop(self):
        flushed = []
        buf = TextBuffer(
            max_size=100,
            flush_delay=10.0,
            on_flush=lambda t: _async_append(flushed, t),
        )
        await buf.flush()
        assert flushed == []

    async def test_append_exactly_at_max(self):
        flushed = []
        buf = TextBuffer(
            max_size=5,
            flush_delay=10.0,
            on_flush=lambda t: _async_append(flushed, t),
        )
        await buf.append("12345")
        assert flushed == ["12345"]

    async def test_delayed_flush_fires(self):
        flushed = []
        buf = TextBuffer(
            max_size=100,
            flush_delay=0.05,
            on_flush=lambda t: _async_append(flushed, t),
        )
        await buf.append("hi")
        assert flushed == []
        await asyncio.sleep(0.1)
        assert flushed == ["hi"]


# --- Bridge.handle_event dispatch tests ---


class TestBridgeHandleEvent:
    async def test_assistant_text_buffers(self):
        bridge = MockBridge()
        await bridge.handle_event("c1", {"type": "assistant_text", "content": "hi"})
        # Text is buffered, not sent yet
        assert len(bridge.calls) == 0
        # Force flush
        await bridge._flush_buffer("c1")
        assert bridge.calls == [("send_text", {"chat_id": "c1", "text": "hi"})]

    async def test_tool_use_flushes_then_sends(self):
        bridge = MockBridge()
        await bridge.handle_event("c1", {"type": "assistant_text", "content": "thinking"})
        await bridge.handle_event(
            "c1",
            {"type": "tool_use", "tool": "Bash", "input": {"command": "ls"}},
        )
        assert len(bridge.calls) == 2
        assert bridge.calls[0] == ("send_text", {"chat_id": "c1", "text": "thinking"})
        assert bridge.calls[1][0] == "send_tool_use"

    async def test_tool_result(self):
        bridge = MockBridge()
        await bridge.handle_event(
            "c1",
            {"type": "tool_result", "output": "file.txt", "is_error": False},
        )
        assert bridge.calls == [
            ("send_tool_result", {"chat_id": "c1", "output": "file.txt", "is_error": False})
        ]

    async def test_tool_approval_request(self):
        bridge = MockBridge()
        await bridge.handle_event(
            "c1",
            {
                "type": "tool_approval_request",
                "tool_use_id": "tu1",
                "tool_name": "Write",
                "tool_input": {"path": "/tmp/f"},
            },
        )
        assert len(bridge.calls) == 1
        assert bridge.calls[0][0] == "send_tool_approval_request"
        assert bridge.calls[0][1]["tool_use_id"] == "tu1"

    async def test_status(self):
        bridge = MockBridge()
        await bridge.handle_event(
            "c1", {"type": "status", "session_id": "s1", "status": "running"}
        )
        assert bridge.calls == [
            ("send_status", {"chat_id": "c1", "status": "running"})
        ]

    async def test_result_flushes_then_sends(self):
        bridge = MockBridge()
        await bridge.handle_event("c1", {"type": "assistant_text", "content": "done"})
        await bridge.handle_event(
            "c1",
            {"type": "result", "session_id": "s1", "cost": 0.01, "is_error": False},
        )
        assert len(bridge.calls) == 2
        assert bridge.calls[0] == ("send_text", {"chat_id": "c1", "text": "done"})
        assert bridge.calls[1] == (
            "send_result",
            {"chat_id": "c1", "cost": 0.01, "is_error": False},
        )

    async def test_error_flushes_then_sends(self):
        bridge = MockBridge()
        await bridge.handle_event("c1", {"type": "assistant_text", "content": "partial"})
        await bridge.handle_event(
            "c1", {"type": "error", "session_id": "s1", "message": "boom"}
        )
        assert len(bridge.calls) == 2
        assert bridge.calls[0] == ("send_text", {"chat_id": "c1", "text": "partial"})
        assert bridge.calls[1] == ("send_error", {"chat_id": "c1", "message": "boom"})

    async def test_user_message_ignored(self):
        bridge = MockBridge()
        await bridge.handle_event(
            "c1", {"type": "user_message", "session_id": "s1", "content": "hello"}
        )
        assert bridge.calls == []

    async def test_unknown_event_ignored(self):
        bridge = MockBridge()
        await bridge.handle_event("c1", {"type": "some_future_type"})
        assert bridge.calls == []


# --- Helpers ---


async def _async_append(lst: list, val: str) -> None:
    lst.append(val)
