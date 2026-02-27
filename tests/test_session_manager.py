import pytest

from server.models import SessionStatus
from server.session_manager import SessionManager


@pytest.fixture
def manager():
    return SessionManager()


def test_create_session(manager):
    session = manager.create_session("Test Session", "/tmp")
    assert session.name == "Test Session"
    assert session.working_dir == "/tmp"
    assert session.status == SessionStatus.idle
    assert len(session.id) == 12
    assert session.id in manager.sessions


def test_create_session_default_dir(manager):
    session = manager.create_session("Default Dir")
    assert session.working_dir == "."


def test_list_sessions(manager):
    assert manager.list_sessions() == []
    manager.create_session("A")
    manager.create_session("B")
    sessions = manager.list_sessions()
    assert len(sessions) == 2
    names = {s.name for s in sessions}
    assert names == {"A", "B"}


def test_get_session(manager):
    session = manager.create_session("Find Me")
    found = manager.get_session(session.id)
    assert found is session
    assert manager.get_session("nonexistent") is None


@pytest.mark.asyncio
async def test_delete_session(manager):
    session = manager.create_session("Delete Me")
    sid = session.id
    assert await manager.delete_session(sid) is True
    assert manager.get_session(sid) is None
    assert await manager.delete_session(sid) is False


@pytest.mark.asyncio
async def test_send_message_unknown_session(manager):
    with pytest.raises(ValueError, match="not found"):
        async for _ in manager.send_message("nonexistent", "hello"):
            pass


def test_broadcast_registration(manager):
    calls = []

    async def cb(msg):
        calls.append(msg)

    manager.on_broadcast(cb)
    assert cb in manager._broadcast_callbacks

    manager.remove_broadcast(cb)
    assert cb not in manager._broadcast_callbacks
