"""Tests for BridgeManager routing and command handling (agent-scoped).

A chat binds durably to an Agent (Default Agent on first contact); inbound
messages route to a sticky session that's created on demand. /new rolls the
thread, /agent rebinds the chat, /switch repoints within the agent. See
docs/plans/agent-refactor.md §5.5 / §8.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from server.bridges.base import Bridge
from server.bridges.manager import BridgeManager
from server.database import Database


# --- Mock Bridge ---


class MockBridge(Bridge):
    name = "mock"

    def __init__(self, manager=None):
        super().__init__(manager=manager or MagicMock())
        self.sent: list[tuple[str, str, dict]] = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send_text(self, chat_id: str, text: str) -> None:
        self.sent.append(("send_text", chat_id, {"text": text}))

    async def send_tool_approval_request(self, chat_id, tool_use_id, tool_name, tool_input):
        self.sent.append(("send_tool_approval_request", chat_id, {}))

    async def send_tool_use(self, chat_id, tool_name, tool_input):
        self.sent.append(("send_tool_use", chat_id, {}))

    async def send_tool_result(self, chat_id, output, is_error):
        self.sent.append(("send_tool_result", chat_id, {}))

    async def send_status(self, chat_id, status):
        self.sent.append(("send_status", chat_id, {}))

    async def send_result(self, chat_id, cost, is_error):
        self.sent.append(("send_result", chat_id, {}))

    async def send_error(self, chat_id, message):
        self.sent.append(("send_error", chat_id, {"message": message}))

    async def send_session_list(self, chat_id, sessions, note=None):
        self.sent.append(
            ("send_session_list", chat_id, {"sessions": sessions, "note": note})
        )


# --- Mock SessionManager (agent-aware) ---


class MockSession:
    def __init__(self, id, name="Test", agent_id=None, origin="user", created_at="2024-01-01T00:00:00Z"):
        self.id = id
        self.name = name
        self.working_dir = "."
        self.status = MagicMock(value="idle")
        self._message_count = 0
        self.agent_id = agent_id
        self.origin = origin
        self.created_at = created_at


class MockSessionManager:
    def __init__(self, db: Database | None = None):
        self._sessions: dict[str, MockSession] = {}
        self._broadcasts: list = []
        self._db = db

    async def create_session(
        self, agent_id, name=None, working_dir=None, credential_id=None, origin="user"
    ):
        import os

        sid = os.urandom(6).hex()
        session = MockSession(sid, name or f"Session {sid[:4]}", agent_id=agent_id, origin=origin)
        self._sessions[sid] = session
        if self._db:
            # Persist so bridge_mappings.session_id FK is satisfied.
            await self._db.save_session(
                sid, session.name, ".", "2024-01-01T00:00:00Z",
                agent_id=agent_id, origin=origin,
            )
        return session

    def get_session(self, session_id):
        return self._sessions.get(session_id)

    def list_sessions(self):
        return list(self._sessions.values())

    async def start_message(self, session_id, prompt):
        pass

    async def send_message(self, session_id, prompt):
        yield {"type": "result", "session_id": session_id, "cost": 0.0, "is_error": False}

    def approve_tool(self, session_id, tool_use_id):
        pass

    def deny_tool(self, session_id, tool_use_id, reason=""):
        pass

    def on_broadcast(self, key, callback):
        self._broadcasts.append(callback)

    def remove_broadcast(self, key):
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


# --- Binding / routing ---


class TestBinding:
    async def test_unbound_message_binds_default_and_opens_session(self, manager, bridge, db):
        await manager.handle_incoming("mock", "c1", "hello", bridge)
        # No dead-end message — a thread was opened on demand.
        assert not any("No session" in str(s) for s in bridge.sent)
        binding = manager._binding("mock", "c1")
        assert binding is not None
        default = await db.get_system_agent()
        assert binding.agent_id == default["id"]
        assert binding.session_id is not None  # sticky session created

    async def test_archived_sticky_opens_fresh(self, manager, bridge, session_mgr):
        await manager.handle_incoming("mock", "c1", "hello", bridge)
        first = manager.get_session_id("mock", "c1")
        # The sticky session disappears (archived/deleted).
        session_mgr._sessions.pop(first)
        bridge.sent.clear()
        await manager.handle_incoming("mock", "c1", "again", bridge)
        second = manager.get_session_id("mock", "c1")
        assert second is not None and second != first


# --- Command tests ---


class TestCommands:
    async def test_cmd_new(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/new My Session", bridge)
        assert "My Session" in bridge.sent[0][2]["text"]
        assert manager.get_session_id("mock", "c1") is not None

    async def test_cmd_new_rolls_thread(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "hello", bridge)
        first = manager.get_session_id("mock", "c1")
        bridge.sent.clear()
        await manager.handle_incoming("mock", "c1", "/new", bridge)
        second = manager.get_session_id("mock", "c1")
        assert second is not None and second != first

    async def test_cmd_agent_rebinds_and_clears_sticky(self, manager, bridge, db):
        now = datetime.now(timezone.utc).isoformat()
        await db.save_agent(agent_id="ag2", name="Helper", created_at=now, updated_at=now)
        await manager.handle_incoming("mock", "c1", "hello", bridge)
        assert manager.get_session_id("mock", "c1") is not None
        bridge.sent.clear()
        await manager.handle_incoming("mock", "c1", "/agent Helper", bridge)
        binding = manager._binding("mock", "c1")
        assert binding.agent_id == "ag2"
        assert binding.session_id is None  # sticky cleared on rebind
        assert "Helper" in bridge.sent[0][2]["text"]

    async def test_cmd_agent_unknown(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/agent nope", bridge)
        assert "not found" in bridge.sent[0][2]["text"]

    async def test_cmd_agent_no_args(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/agent", bridge)
        assert "Usage" in bridge.sent[0][2]["text"]

    async def test_cmd_sessions_empty(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/sessions", bridge)
        assert "No sessions" in bridge.sent[0][2]["text"]

    async def test_cmd_sessions_lists(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/new First", bridge)
        bridge.sent.clear()
        await manager.handle_incoming("mock", "c1", "/sessions", bridge)
        kind, _, payload = bridge.sent[0]
        assert kind == "send_session_list"
        names = [s["name"] for s in payload["sessions"]]
        assert "First" in names
        current = [s for s in payload["sessions"] if s["current"]]
        assert current and current[0]["name"] == "First"
        assert payload["note"] is None

    async def test_cmd_sessions_truncates_to_limit(self, manager, bridge, session_mgr):
        agent_id = await manager._ensure_bound("mock", "c1")
        for i in range(manager.SESSION_LIST_LIMIT + 5):
            await session_mgr.create_session(agent_id, name=f"S{i}")
        bridge.sent.clear()
        await manager.handle_incoming("mock", "c1", "/sessions", bridge)
        payload = bridge.sent[0][2]
        assert len(payload["sessions"]) == manager.SESSION_LIST_LIMIT
        assert payload["note"] is not None
        assert "/switch" in payload["note"]

    async def test_cmd_switch(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/new First", bridge)
        first_sid = manager.get_session_id("mock", "c1")
        await manager.handle_incoming("mock", "c1", "/new Second", bridge)
        bridge.sent.clear()
        await manager.handle_incoming("mock", "c1", f"/switch {first_sid}", bridge)
        assert "Switched" in bridge.sent[0][2]["text"]
        assert manager.get_session_id("mock", "c1") == first_sid

    async def test_cmd_switch_other_agent_rejected(self, manager, bridge, session_mgr, db):
        # A session under a different agent can't be switched to.
        now = datetime.now(timezone.utc).isoformat()
        await db.save_agent(agent_id="ag-other", name="Foreign", created_at=now, updated_at=now)
        other = await session_mgr.create_session("ag-other", name="Foreign")
        await manager.handle_incoming("mock", "c1", "hello", bridge)  # binds default
        bridge.sent.clear()
        await manager.handle_incoming("mock", "c1", f"/switch {other.id}", bridge)
        assert "different agent" in bridge.sent[0][2]["text"]

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
        assert "/agent" in text
        assert "/quiet" in text
        assert "/verbose" in text

    async def test_cmd_unknown(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/foo", bridge)
        assert "Unknown command" in bridge.sent[0][2]["text"]


# --- Persistence ---


class TestMappings:
    async def test_persist_and_reload(self, session_mgr, db):
        mgr1 = BridgeManager(session_mgr, db)
        await mgr1.initialize()
        await mgr1.handle_incoming("mock", "c1", "hi", MockBridge(mgr1))
        b1 = mgr1._binding("mock", "c1")

        mgr2 = BridgeManager(session_mgr, db)
        await mgr2.initialize()
        b2 = mgr2._binding("mock", "c1")
        assert (b2.agent_id, b2.session_id) == (b1.agent_id, b1.session_id)

    async def test_remove_mapping(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "hi", bridge)
        assert manager._binding("mock", "c1") is not None
        await manager.remove_mapping("mock", "c1")
        assert manager._binding("mock", "c1") is None


# --- Broadcast routing ---


class TestBroadcast:
    async def test_broadcast_routes_to_sticky_chat(self, manager, bridge):
        # An always-passed event (error) reaches the sticky chat regardless
        # of verbosity.
        await manager.handle_incoming("mock", "c1", "/new Test", bridge)
        sid = manager.get_session_id("mock", "c1")
        bridge.sent.clear()
        await manager._on_broadcast(
            {"type": "error", "session_id": sid, "message": "boom"}
        )
        assert len(bridge.sent) == 1
        assert bridge.sent[0][0] == "send_error"

    async def test_broadcast_ignores_unmapped(self, manager, bridge):
        await manager._on_broadcast(
            {"type": "error", "session_id": "other", "message": "boom"}
        )
        assert len(bridge.sent) == 0

    async def test_broadcast_no_session_id(self, manager, bridge):
        await manager._on_broadcast({"type": "error", "message": "boom"})
        assert len(bridge.sent) == 0


class TestVerbosity:
    @pytest.mark.parametrize(
        "event",
        [
            {"type": "tool_use", "tool": "Bash", "input": {}},
            {"type": "tool_result", "output": "x", "is_error": False},
            {"type": "result", "cost": 0.01, "is_error": False},
            {"type": "status", "status": "running"},
        ],
    )
    async def test_quiet_default_suppresses_tool_events(self, manager, bridge, event):
        await manager.handle_incoming("mock", "c1", "/new Test", bridge)
        sid = manager.get_session_id("mock", "c1")
        bridge.sent.clear()
        await manager._on_broadcast({**event, "session_id": sid})
        assert bridge.sent == []

    async def test_quiet_forwards_octo_text(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/new Test", bridge)
        sid = manager.get_session_id("mock", "c1")
        bridge.sent.clear()
        await manager._on_broadcast(
            {"type": "assistant_text", "session_id": sid, "content": "hi there"}
        )
        # assistant_text is buffered; force a flush to observe the send.
        await bridge._flush_buffer("c1")
        assert bridge.sent == [("send_text", "c1", {"text": "hi there"})]

    async def test_verbose_forwards_tool_events(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/new Test", bridge)
        await manager.handle_incoming("mock", "c1", "/verbose", bridge)
        sid = manager.get_session_id("mock", "c1")
        bridge.sent.clear()
        await manager._on_broadcast(
            {"type": "tool_use", "session_id": sid, "tool": "Bash", "input": {}}
        )
        assert len(bridge.sent) == 1
        assert bridge.sent[0][0] == "send_tool_use"

    async def test_cmd_verbose_then_quiet_toggles(self, manager, bridge):
        await manager.handle_incoming("mock", "c1", "/verbose", bridge)
        assert manager._binding("mock", "c1").verbose is True
        assert "Verbose" in bridge.sent[-1][2]["text"]
        await manager.handle_incoming("mock", "c1", "/quiet", bridge)
        assert manager._binding("mock", "c1").verbose is False
        assert "Quiet" in bridge.sent[-1][2]["text"]

    async def test_verbose_persists_across_reload(self, session_mgr, db):
        mgr1 = BridgeManager(session_mgr, db)
        await mgr1.initialize()
        await mgr1.handle_incoming("mock", "c1", "/verbose", MockBridge(mgr1))
        assert mgr1._binding("mock", "c1").verbose is True

        mgr2 = BridgeManager(session_mgr, db)
        await mgr2.initialize()
        assert mgr2._binding("mock", "c1").verbose is True

    async def test_verbose_survives_agent_rebind(self, manager, bridge, db):
        now = datetime.now(timezone.utc).isoformat()
        await db.save_agent(agent_id="ag2", name="Helper", created_at=now, updated_at=now)
        await manager.handle_incoming("mock", "c1", "/verbose", bridge)
        await manager.handle_incoming("mock", "c1", "/agent Helper", bridge)
        assert manager._binding("mock", "c1").verbose is True
