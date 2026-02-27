import pytest

from server.database import Database
from server.models import SessionStatus
from server.session_manager import SessionManager


@pytest.fixture
async def manager():
    mgr = SessionManager()
    db = Database(":memory:")
    await db.initialize()
    await mgr.initialize(db)
    return mgr


@pytest.mark.asyncio
async def test_create_session(manager):
    session = await manager.create_session("Test Session", "/tmp")
    assert session.name == "Test Session"
    assert session.working_dir == "/tmp"
    assert session.status == SessionStatus.idle
    assert len(session.id) == 12
    assert session.id in manager.sessions


@pytest.mark.asyncio
async def test_create_session_default_dir(manager):
    session = await manager.create_session("Default Dir")
    assert session.working_dir == "."


@pytest.mark.asyncio
async def test_list_sessions(manager):
    assert manager.list_sessions() == []
    await manager.create_session("A")
    await manager.create_session("B")
    sessions = manager.list_sessions()
    assert len(sessions) == 2
    names = {s.name for s in sessions}
    assert names == {"A", "B"}


@pytest.mark.asyncio
async def test_get_session(manager):
    session = await manager.create_session("Find Me")
    found = manager.get_session(session.id)
    assert found is session
    assert manager.get_session("nonexistent") is None


@pytest.mark.asyncio
async def test_delete_session(manager):
    session = await manager.create_session("Delete Me")
    sid = session.id
    assert await manager.delete_session(sid) is True
    assert manager.get_session(sid) is None
    assert await manager.delete_session(sid) is False


@pytest.mark.asyncio
async def test_send_message_unknown_session(manager):
    with pytest.raises(ValueError, match="not found"):
        async for _ in manager.send_message("nonexistent", "hello"):
            pass


@pytest.mark.asyncio
async def test_broadcast_registration(manager):
    calls = []

    async def cb(msg):
        calls.append(msg)

    manager.on_broadcast(cb)
    assert cb in manager._broadcast_callbacks

    manager.remove_broadcast(cb)
    assert cb not in manager._broadcast_callbacks


@pytest.mark.asyncio
async def test_create_session_persists_to_db(manager):
    session = await manager.create_session("Persisted", "/home")
    rows = await manager.db.load_sessions()
    assert any(r["id"] == session.id for r in rows)


@pytest.mark.asyncio
async def test_delete_session_removes_from_db(manager):
    session = await manager.create_session("To Delete", "/tmp")
    sid = session.id
    await manager.delete_session(sid)
    rows = await manager.db.load_sessions()
    assert not any(r["id"] == sid for r in rows)


@pytest.mark.asyncio
async def test_initialize_restores_sessions():
    """Create a session with one manager, then load into a fresh manager."""
    db = Database(":memory:")
    await db.initialize()

    mgr1 = SessionManager()
    await mgr1.initialize(db)
    session = await mgr1.create_session("Restored", "/tmp")
    sid = session.id

    # Create a fresh manager, initialize with the same DB
    mgr2 = SessionManager()
    await mgr2.initialize(db)
    restored = mgr2.get_session(sid)
    assert restored is not None
    assert restored.name == "Restored"
    assert restored.working_dir == "/tmp"
    assert restored.status == SessionStatus.idle
