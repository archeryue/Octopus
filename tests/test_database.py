"""Tests for SQLite persistence layer."""

import pytest

from server.database import Database


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_initialize_creates_tables(db):
    cursor = await db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row[0] for row in await cursor.fetchall()}
    assert "sessions" in tables
    assert "messages" in tables


@pytest.mark.asyncio
async def test_save_and_load_session(db):
    await db.save_session(
        session_id="abc123",
        name="Test Session",
        working_dir="/tmp",
        created_at="2025-01-01T00:00:00+00:00",
        claude_session_id=None,
    )
    sessions = await db.load_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s["id"] == "abc123"
    assert s["name"] == "Test Session"
    assert s["working_dir"] == "/tmp"
    assert s["created_at"] == "2025-01-01T00:00:00+00:00"
    assert s["claude_session_id"] is None


@pytest.mark.asyncio
async def test_delete_session_cascade(db):
    await db.save_session("s1", "Session 1", "/tmp", "2025-01-01T00:00:00+00:00")
    await db.append_message("s1", seq=0, role="user", type="text", content="hello")
    await db.append_message("s1", seq=1, role="assistant", type="text", content="hi")

    await db.delete_session("s1")

    sessions = await db.load_sessions()
    assert len(sessions) == 0
    messages = await db.load_messages("s1")
    assert len(messages) == 0


@pytest.mark.asyncio
async def test_append_and_load_messages(db):
    await db.save_session("s1", "Session 1", "/tmp", "2025-01-01T00:00:00+00:00")

    await db.append_message("s1", seq=0, role="user", type="text", content="hello")
    await db.append_message(
        "s1",
        seq=1,
        role="assistant",
        type="tool_use",
        tool_name="Read",
        tool_input={"path": "/tmp/foo"},
        tool_use_id="tu_123",
    )
    await db.append_message(
        "s1",
        seq=2,
        role="tool",
        type="tool_result",
        content="file contents",
        tool_use_id="tu_123",
        is_error=False,
    )

    messages = await db.load_messages("s1")
    assert len(messages) == 3

    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "hello"

    assert messages[1]["role"] == "assistant"
    assert messages[1]["type"] == "tool_use"
    assert messages[1]["tool_name"] == "Read"
    assert messages[1]["tool_input"] == {"path": "/tmp/foo"}
    assert messages[1]["tool_use_id"] == "tu_123"

    assert messages[2]["role"] == "tool"
    assert messages[2]["is_error"] is False
    assert messages[2]["content"] == "file contents"


@pytest.mark.asyncio
async def test_update_session_field(db):
    await db.save_session("s1", "Session 1", "/tmp", "2025-01-01T00:00:00+00:00")
    await db.update_session_field("s1", claude_session_id="claude_abc")

    sessions = await db.load_sessions()
    assert sessions[0]["claude_session_id"] == "claude_abc"


@pytest.mark.asyncio
async def test_load_sessions_on_restart(db):
    """Simulate restart: save sessions and messages, create new Database, verify restored."""
    await db.save_session("s1", "Session A", "/home", "2025-01-01T00:00:00+00:00")
    await db.save_session("s2", "Session B", "/tmp", "2025-01-02T00:00:00+00:00")
    await db.append_message("s1", seq=0, role="user", type="text", content="msg1")
    await db.append_message("s1", seq=1, role="assistant", type="text", content="reply1")
    await db.append_message("s2", seq=0, role="user", type="text", content="msg2")

    # Simulate reading back (same connection, but tests the load path)
    sessions = await db.load_sessions()
    assert len(sessions) == 2

    s1_msgs = await db.load_messages("s1")
    assert len(s1_msgs) == 2
    assert s1_msgs[0]["content"] == "msg1"
    assert s1_msgs[1]["content"] == "reply1"

    s2_msgs = await db.load_messages("s2")
    assert len(s2_msgs) == 1
    assert s2_msgs[0]["content"] == "msg2"


@pytest.mark.asyncio
async def test_update_ignores_unknown_fields(db):
    await db.save_session("s1", "Session 1", "/tmp", "2025-01-01T00:00:00+00:00")
    # Should not raise even with unknown fields
    await db.update_session_field("s1", status="running", claude_session_id="abc")
    sessions = await db.load_sessions()
    assert sessions[0]["claude_session_id"] == "abc"


@pytest.mark.asyncio
async def test_message_with_cost_and_session_id(db):
    await db.save_session("s1", "Session 1", "/tmp", "2025-01-01T00:00:00+00:00")
    await db.append_message(
        "s1",
        seq=0,
        role="system",
        type="result",
        session_id_ref="claude_xyz",
        cost=0.05,
    )
    messages = await db.load_messages("s1")
    assert len(messages) == 1
    assert messages[0]["session_id"] == "claude_xyz"
    assert messages[0]["cost"] == pytest.approx(0.05)
