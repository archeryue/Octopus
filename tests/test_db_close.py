"""Closing the database has to unwind what is still using it.

Both halves here were found the same way: an intermittently *hanging* test run —
not a failing one. A turn cancelled after its connection has gone runs its own
`finally`, which flushes and broadcasts, has nothing to await, and cannot be
cancelled out of the attempt; the event loop then stops making progress. What
you see is a suite that passes twice and stalls forever on the third run.
"""

from __future__ import annotations

import asyncio

import pytest

from server.database import Database
from server.session_manager import SessionManager


@pytest.mark.asyncio
async def test_close_hooks_run_while_the_connection_still_answers():
    """A hook is for work that needs the database one last time."""
    db = Database(":memory:")
    await db.initialize()
    seen: list[int] = []

    async def hook() -> None:
        cursor = await db.conn.execute("SELECT COUNT(*) FROM sessions")
        seen.append((await cursor.fetchone())[0])

    db.add_close_hook(hook)
    await db.close()
    assert seen == [0], "the hook could not reach the database"


@pytest.mark.asyncio
async def test_close_hooks_unwind_in_reverse_and_survive_one_that_raises():
    db = Database(":memory:")
    await db.initialize()
    order: list[str] = []

    async def first() -> None:
        order.append("first")

    async def boom() -> None:
        order.append("boom")
        raise RuntimeError("no")

    db.add_close_hook(first)
    db.add_close_hook(boom)
    await db.close()

    assert order == ["boom", "first"], order
    # And the connection still closed, which is the point of catching it.
    with pytest.raises(asyncio.CancelledError):
        await db._ensure_connected()


@pytest.mark.asyncio
async def test_a_closed_database_is_closed_before_its_connection_goes():
    """The flag order, which is the race on its own.

    `await conn.close()` yields. A task that reaches `_ensure_connected` in that
    window used to pass the check and submit work to a worker thread that was
    stopping — a future nothing would ever resolve.
    """
    db = Database(":memory:")
    await db.initialize()

    closed_during_hook: list[bool] = []

    async def hook() -> None:
        # Hooks run *before* the flag, so they can still use the connection.
        closed_during_hook.append(db._closed)

    db.add_close_hook(hook)
    await db.close()

    assert closed_during_hook == [False]
    assert db._closed is True
    with pytest.raises(asyncio.CancelledError):
        await db._ensure_connected()


@pytest.mark.asyncio
async def test_the_session_manager_drains_its_turns_when_the_db_closes():
    """The hook that matters: a turn in flight is stopped while it can still
    unwind, rather than cancelled into a connection that has gone."""
    db = Database(":memory:")
    await db.initialize()
    mgr = SessionManager()
    mgr.sessions.clear()
    await mgr.initialize(db)

    agent = await db.get_system_agent()
    session = await mgr.create_session(agent["id"], "work", "/tmp")

    started = asyncio.Event()
    unwound = asyncio.Event()

    async def never_ends() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            # A real turn flushes and broadcasts here; all this has to prove is
            # that the unwinding happens at all, and before close returns.
            unwound.set()

    session._active_task = asyncio.create_task(never_ends())
    await started.wait()

    await db.close()

    assert unwound.is_set(), "the turn was not drained before the DB closed"
    assert session._active_task.done()
    mgr.sessions.clear()
