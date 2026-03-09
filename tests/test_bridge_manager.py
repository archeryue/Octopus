"""Tests for BridgeManager routing and command handling."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.bridges.base import Bridge
from server.bridges.manager import BridgeManager
from server.database import Database


# --- Mock Bridge ---


class MockBridge(Bridge):
    name = "mock"

    def __init__(self, manager=None):
        super().__init__(manager=manager or MagicMock())
        self.sent: list[tuple[str, str, dict]] = []  # (method, chat_id, kwargs)

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send_text(self, chat_id: str, text: str) -> None:
        self.sent.append(("send_text", chat_id, {"text": text}))

    async def send_tool_approval_request(
        self, chat_id: str, tool_use_id: str, tool_name: str, tool_input: dict
    ) -> None:
        self.sent.append(("send_tool_approval_request", chat_id, {}))

    async def send_tool_use(
        self, chat_id: str, tool_name: str, tool_input: dict
    ) -> None:
        self.sent.append(("send_tool_use", chat_id, {}))

    async def send_tool_result(
        self, chat_id: str, output: str, is_error: bool
    ) -> None:
        self.sent.append(("send_tool_result", chat_id, {}))

    async def send_status(self, chat_id: str, status: str) -> None:
        self.sent.append(("send_status", chat_id, {}))

    async def send_result(
        self, chat_id: str, cost: float | None, is_error: bool
    ) -> None:
        self.sent.append(("send_result", chat_id, {}))

    async def send_error(self, chat_id: str, message: str) -> None:
        self.sent.append(("send_error", chat_id, {"message": message}))


# --- Mock SessionManager ---


class MockSession:
    def __init__(self, id: str, name: str = "Test", status_val: str = "idle"):
        self.id = id
        self.name = name
        self.working_dir = "."
        self.status = MagicMock(value=status_val)
        self._message_count = 0


class MockSessionManager:
    def __init__(self, db: Database | None = None):
        self._sessions: dict[str, MockSession] = {}
        self._broadcasts: list = []
        self._db = db

    async def create_session(self, name: str, working_dir: str | None = None):
        import os
        session = MockSession(os.urandom(6).hex(), name)
        self._sessions[session.id] = session
        if self._db:
            await self._db.save_session(session.id, name, ".", "2024-01-01T00:00:00Z")
        return session

    def get_session(self, session_id: str):
        return self._sessions.get(session_id)

    def list_sessions(self):
        return list(self._sessions.values())

    async def start_message(self, session_id: str, prompt: str):
        pass  # In tests, events reach bridges via broadcast

    async def send_message(self, session_id: str, prompt: str):
        yield {"type": "assistant_text", "session_id": session_id, "content": "reply"}
        yield {"type": "result", "session_id": session_id, "cost": 0.001, "is_error": False}

    def approve_tool(self, session_id: str, tool_use_id: str):
        pass

    def deny_tool(self, session_id: str, tool_use_id: str, reason: str = ""):
        pass

    def on_broadcast(self, key: str, callback):
        self._broadcasts.append(callback)

    def remove_broadcast(self, key: str):
        if self._broadcasts:
            self._broadcasts.pop()


# --- Fixtures ---


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def session_mgr(db):
    return MockSessionManager(db)


@pytest.fixture
async def manager(session_mgr, db):
    mgr = BridgeManager(session_mgr, db)
    await mgr.initialize()
    return mgr


@pytest.fixture
def bridge(manager):
    b = MockBridge(manager)
    manager.register_bridge(b)
    return b


# --- Command tests ---


class TestCommands:
    async def test_cmd_new(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/new My Session", bridge)
        assert len(bridge.sent) == 1
        assert bridge.sent[0][0] == "send_text"
        assert "My Session" in bridge.sent[0][2]["text"]
        # Verify mapping was created
        sid = manager.get_session_id("mock", "c1")
        assert sid is not None

    async def test_cmd_new_default_name(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/new", bridge)
        assert "Bridge Session" in bridge.sent[0][2]["text"]

    async def test_cmd_sessions_empty(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/sessions", bridge)
        assert "No sessions" in bridge.sent[0][2]["text"]

    async def test_cmd_sessions_lists(self, manager, bridge, session_mgr):
        await manager.handle_incoming("mock", "c1", "/new First", bridge)
        bridge.sent.clear()
        await manager.handle_incoming("mock", "c1", "/sessions", bridge)
        text = bridge.sent[0][2]["text"]
        assert "First" in text
        assert "(current)" in text

    async def test_cmd_switch(self, manager, bridge, session_mgr):
        await manager.handle_incoming("mock", "c1", "/new First", bridge)
        first_sid = manager.get_session_id("mock", "c1")
        await manager.handle_incoming("mock", "c1", "/new Second", bridge)
        bridge.sent.clear()

        await manager.handle_incoming("mock", "c1", f"/switch {first_sid}", bridge)
        assert "Switched" in bridge.sent[0][2]["text"]
        assert manager.get_session_id("mock", "c1") == first_sid

    async def test_cmd_switch_not_found(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/switch nonexistent", bridge)
        assert "not found" in bridge.sent[0][2]["text"]

    async def test_cmd_switch_no_args(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/switch", bridge)
        assert "Usage" in bridge.sent[0][2]["text"]

    async def test_cmd_current_no_session(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/current", bridge)
        assert "No session" in bridge.sent[0][2]["text"]

    async def test_cmd_current_shows_info(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/new My Sess", bridge)
        bridge.sent.clear()
        await manager.handle_incoming("mock", "c1", "/current", bridge)
        text = bridge.sent[0][2]["text"]
        assert "My Sess" in text
        assert "idle" in text

    async def test_cmd_help(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/help", bridge)
        text = bridge.sent[0][2]["text"]
        assert "/new" in text
        assert "/sessions" in text

    async def test_cmd_unknown(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/foo", bridge)
        assert "Unknown command" in bridge.sent[0][2]["text"]


# --- Mapping tests ---


class TestMappings:
    async def test_set_and_get(self, manager, session_mgr):
        session = await session_mgr.create_session("test")
        await manager.set_mapping("telegram", "123", session.id)
        assert manager.get_session_id("telegram", "123") == session.id

    async def test_remove_mapping(self, manager, session_mgr):
        session = await session_mgr.create_session("test")
        await manager.set_mapping("telegram", "123", session.id)
        await manager.remove_mapping("telegram", "123")
        assert manager.get_session_id("telegram", "123") is None

    async def test_persist_and_reload(self, session_mgr, db):
        mgr1 = BridgeManager(session_mgr, db)
        await mgr1.initialize()

        session = await session_mgr.create_session("test")
        await mgr1.set_mapping("telegram", "123", session.id)

        # New manager loads from DB
        mgr2 = BridgeManager(session_mgr, db)
        await mgr2.initialize()
        assert mgr2.get_session_id("telegram", "123") == session.id


# --- Message routing tests ---


class TestRouting:
    async def test_no_session_prompts_user(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "hello", bridge)
        assert "No session" in bridge.sent[0][2]["text"]

    async def test_message_starts_session_task(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/new Test", bridge)
        bridge.sent.clear()

        # Message should call start_message (no error = success)
        await manager.handle_incoming("mock", "c1", "hello world", bridge)
        # No error messages should be sent
        error_msgs = [s for s in bridge.sent if "Error" in str(s)]
        assert len(error_msgs) == 0

    async def test_broadcast_routes_events_to_bridge(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/new Test", bridge)
        bridge.sent.clear()

        session_id = manager.get_session_id("mock", "c1")
        # Simulate broadcast event from session manager
        await manager._on_broadcast({
            "type": "result", "session_id": session_id,
            "cost": 0.001, "is_error": False,
        })

        methods = [s[0] for s in bridge.sent]
        assert "send_result" in methods

    async def test_stale_mapping_cleaned(self, manager, bridge, session_mgr):
        # Set mapping to non-existent session
        manager._mappings["mock:c1"] = "nonexistent"
        await manager.handle_incoming("mock", "c1", "hello", bridge)
        assert "no longer exists" in bridge.sent[0][2]["text"]
        assert manager.get_session_id("mock", "c1") is None


# --- Broadcast tests ---


class TestBroadcast:
    async def test_broadcast_routes_to_mapped_chats(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/new Test", bridge)
        sid = manager.get_session_id("mock", "c1")
        bridge.sent.clear()

        await manager._on_broadcast(
            {"type": "status", "session_id": sid, "status": "running"}
        )
        assert len(bridge.sent) == 1
        assert bridge.sent[0][0] == "send_status"

    async def test_broadcast_ignores_unmapped(self, manager, bridge):
        await manager._on_broadcast(
            {"type": "status", "session_id": "other", "status": "running"}
        )
        assert len(bridge.sent) == 0

    async def test_broadcast_no_session_id(self, manager, bridge):
        await manager._on_broadcast({"type": "status"})
        assert len(bridge.sent) == 0
