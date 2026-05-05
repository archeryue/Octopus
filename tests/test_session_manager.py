import asyncio

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

    manager.on_broadcast("test", cb)
    assert "test" in manager._broadcast_callbacks

    manager.remove_broadcast("test")
    assert "test" not in manager._broadcast_callbacks


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


# ---------------------------------------------------------------------------
# Message queue + interrupt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_message_queues_when_busy(manager, monkeypatch):
    session = await manager.create_session("Q")
    consumed: list[str] = []
    blocker = asyncio.Event()

    async def stub_consume(session_id: str, prompt: str) -> None:
        consumed.append(prompt)
        if len(consumed) == 1:
            await blocker.wait()

    monkeypatch.setattr(manager, "_consume_message", stub_consume)

    events: list[dict] = []

    async def cb(msg: dict) -> None:
        events.append(msg)

    manager.on_broadcast("test", cb)

    await manager.start_message(session.id, "first")
    # Yield so the orchestrator + first stub_consume get a chance to start
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Second start_message should queue rather than fire
    await manager.start_message(session.id, "second")
    assert session._pending_queue == ["second"]

    queued = [e for e in events if e["type"] == "queued"]
    assert len(queued) == 1
    assert queued[0]["content"] == "second"
    assert queued[0]["queue_length"] == 1

    # Release the blocker; orchestrator should drain the queue
    blocker.set()
    await asyncio.wait_for(session._active_task, timeout=2)

    assert consumed == ["first", "second"]
    assert session._pending_queue == []
    assert any(e["type"] == "dequeued" for e in events)


@pytest.mark.asyncio
async def test_interrupt_cancels_current_and_advances_queue(manager, monkeypatch):
    session = await manager.create_session("I")
    started: list[str] = []
    cancelled: list[str] = []

    async def stub_consume(session_id: str, prompt: str) -> None:
        started.append(prompt)
        try:
            # Block forever so interrupt() must cancel us
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(prompt)
            raise

    monkeypatch.setattr(manager, "_consume_message", stub_consume)

    await manager.start_message(session.id, "first")
    # Wait until the inner task is scheduled and started
    for _ in range(20):
        if started:
            break
        await asyncio.sleep(0.01)
    assert started == ["first"]

    await manager.start_message(session.id, "second")
    assert session._pending_queue == ["second"]

    ok = await manager.interrupt(session.id)
    assert ok is True

    # Allow the orchestrator to pick up the dequeued prompt
    for _ in range(50):
        if "second" in started:
            break
        await asyncio.sleep(0.01)

    assert started == ["first", "second"]
    assert cancelled == ["first"]
    assert session._pending_queue == []

    # Cleanup: cancel the second so the test doesn't hang
    await manager.interrupt(session.id)
    try:
        await asyncio.wait_for(session._active_task, timeout=1)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass


@pytest.mark.asyncio
async def test_interrupt_twice_in_a_row_each_works(manager, monkeypatch):
    """Reproduces the bug where pressing Esc to interrupt a queued message
    that just started running was a no-op."""
    session = await manager.create_session("DoubleInterrupt")
    started: list[str] = []
    cancelled: list[str] = []

    async def stub_consume(session_id: str, prompt: str) -> None:
        started.append(prompt)
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(prompt)
            raise

    monkeypatch.setattr(manager, "_consume_message", stub_consume)

    await manager.start_message(session.id, "first")
    for _ in range(50):
        if started:
            break
        await asyncio.sleep(0.01)
    assert started == ["first"]

    await manager.start_message(session.id, "second")
    assert session._pending_queue == ["second"]

    # First interrupt
    assert await manager.interrupt(session.id) is True

    # Wait for the queue to advance and "second" to start
    for _ in range(100):
        if "second" in started:
            break
        await asyncio.sleep(0.01)
    assert started == ["first", "second"]
    assert cancelled == ["first"]

    # Second interrupt — this is the bug repro: must also succeed
    assert await manager.interrupt(session.id) is True

    for _ in range(100):
        if "second" in cancelled:
            break
        await asyncio.sleep(0.01)
    assert cancelled == ["first", "second"]
    assert session._pending_queue == []

    try:
        await asyncio.wait_for(session._active_task, timeout=1)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass


@pytest.mark.asyncio
async def test_interrupt_does_not_wedge_on_slow_disconnect(manager, monkeypatch):
    """If the SDK client's disconnect() hangs, interrupt() must still
    return promptly (within the timeout) so the WS receive loop isn't
    blocked from processing subsequent interrupts."""
    session = await manager.create_session("SlowDisconnect")

    async def stub_consume(session_id: str, prompt: str) -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise

    class HangingClient:
        async def disconnect(self):
            await asyncio.sleep(60)  # would hang interrupt() if not timed out

    monkeypatch.setattr(manager, "_consume_message", stub_consume)
    await manager.start_message(session.id, "x")
    for _ in range(20):
        if session._inner_task and not session._inner_task.done():
            break
        await asyncio.sleep(0.01)

    # Plant the hanging client on the session
    session._client = HangingClient()  # type: ignore[assignment]

    # interrupt() must return within the disconnect timeout (2s) + a margin
    try:
        ok = await asyncio.wait_for(manager.interrupt(session.id), timeout=4.0)
    except asyncio.TimeoutError:
        pytest.fail("interrupt() blocked on hanging disconnect — WS would be wedged")

    assert ok is True

    try:
        await asyncio.wait_for(session._active_task, timeout=2)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass


@pytest.mark.asyncio
async def test_interrupt_when_idle_returns_false(manager):
    session = await manager.create_session("Idle")
    assert await manager.interrupt(session.id) is False


@pytest.mark.asyncio
async def test_delete_session_clears_queue(manager, monkeypatch):
    session = await manager.create_session("Del")
    blocker = asyncio.Event()

    async def stub_consume(session_id: str, prompt: str) -> None:
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(manager, "_consume_message", stub_consume)

    await manager.start_message(session.id, "first")
    await asyncio.sleep(0)
    await manager.start_message(session.id, "second")
    assert session._pending_queue == ["second"]

    await manager.delete_session(session.id)
    assert session._pending_queue == []
    assert session._inner_task is None or session._inner_task.cancelled() or session._inner_task.done()
