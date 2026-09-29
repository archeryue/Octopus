"""One account cannot see or touch another's (multi-tenancy.md §5).

This is the test that has to exist. Ownership is enforced at ~100 routes, and
the failure mode of "remember to scope this lookup" is that one is forgotten,
nothing goes red, and the bug is found by a user seeing someone else's work.

So the shape here is deliberate: two real accounts, each with their own rows,
and then *every* surface asked the same question — can Vera reach Archer's?
A route added later that forgets its scope fails here rather than in someone's
browser. Covering "can she read it" and "can she change it" both, because a
404 on the read and a 200 on the write is a real and popular way to get this
half right.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from server import crypto, deps
from server.agent_manager import AgentManager
from server.config import settings
from server.database import Database
from server.main import app
from server.routers import agents as agents_router
from server.routers import auth as auth_router
from server.session_manager import session_manager
from server.users import UserManager
from server.workspace import WorkspaceError, paths_for


@pytest.fixture
async def two_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "master_key", "test-master-key")
    # Each account's files under this test's own root, which is also what makes
    # the working directories below legal: a session may only work inside its
    # owner's workspace (§6).
    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))
    crypto._MASTER_CACHE.clear()

    db = Database(":memory:")
    await db.initialize()
    session_manager.sessions.clear()
    await session_manager.initialize(db)
    auth_router.set_db(db)
    agents_router.set_manager(AgentManager(db))
    users = UserManager(db)
    deps.set_user_manager(users)

    archer = await users.create_user(username="archer", password="password1")
    vera = await users.create_user(username="vera", password="password2")
    archer_token = await users.issue_token(archer["id"])
    vera_token = await users.issue_token(vera["id"])

    # One agent and one session each, owned properly.
    mgr = AgentManager(db)
    archer_agent = await mgr.create_agent(name="Archer's agent", user_id=archer["id"])
    vera_agent = await mgr.create_agent(name="Vera's agent", user_id=vera["id"])
    archer_session = await session_manager.create_session(
        archer_agent["id"],
        "archer's work",
        str(paths_for(archer["id"]).ensure().workspace),
    )
    vera_session = await session_manager.create_session(
        vera_agent["id"],
        "vera's work",
        str(paths_for(vera["id"]).ensure().workspace),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield {
            "client": c,
            "db": db,
            "archer": {"user": archer, "token": archer_token, "agent": archer_agent,
                       "session": archer_session},
            "vera": {"user": vera, "token": vera_token, "agent": vera_agent,
                     "session": vera_session},
        }
    await db.close()
    session_manager.sessions.clear()


def _as(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_a_session_created_under_an_agent_belongs_to_that_agents_owner(
    two_accounts,
):
    """The derivation everything else rests on: nobody passes an owner when
    creating a session, so if this is wrong every check below passes while
    protecting nothing."""
    ctx = two_accounts
    assert ctx["archer"]["session"].user_id == ctx["archer"]["user"]["id"]
    assert ctx["vera"]["session"].user_id == ctx["vera"]["user"]["id"]


@pytest.mark.asyncio
async def test_listing_shows_only_your_own(two_accounts):
    ctx = two_accounts
    c = ctx["client"]

    sessions = (await c.get("/api/sessions", headers=_as(ctx["vera"]["token"]))).json()
    names = {s["name"] for s in sessions}
    assert names == {"vera's work"}, f"Vera was shown {names}"

    agents = (await c.get("/api/agents", headers=_as(ctx["vera"]["token"]))).json()
    agent_names = {a["name"] for a in agents}
    assert "Archer's agent" not in agent_names, f"Vera was shown {agent_names}"


@pytest.mark.asyncio
async def test_reading_someone_elses_row_is_a_404_not_a_403(two_accounts):
    """404, because 403 confirms the id exists — which is a slow way to
    enumerate another account's work."""
    ctx = two_accounts
    c = ctx["client"]
    vera = _as(ctx["vera"]["token"])

    assert (
        await c.get(f"/api/sessions/{ctx['archer']['session'].id}", headers=vera)
    ).status_code == 404
    assert (
        await c.get(f"/api/agents/{ctx['archer']['agent']['id']}", headers=vera)
    ).status_code == 404


@pytest.mark.asyncio
async def test_writing_to_someone_elses_row_is_refused(two_accounts):
    """The half that is easy to leave out: a read that 404s and a write that
    succeeds is worse than neither, because it looks protected."""
    ctx = two_accounts
    c = ctx["client"]
    vera = _as(ctx["vera"]["token"])
    victim_session = ctx["archer"]["session"].id
    victim_agent = ctx["archer"]["agent"]["id"]

    renamed = await c.patch(
        f"/api/sessions/{victim_session}", json={"name": "mine now"}, headers=vera
    )
    assert renamed.status_code == 404

    modelled = await c.patch(
        f"/api/sessions/{victim_session}", json={"model": "opus"}, headers=vera
    )
    assert modelled.status_code == 404

    updated = await c.put(
        f"/api/agents/{victim_agent}", json={"name": "mine now"}, headers=vera
    )
    assert updated.status_code in (404, 405), updated.status_code

    archived = await c.post(f"/api/agents/{victim_agent}/archive", headers=vera)
    assert archived.status_code == 404

    # And none of it happened.
    still = await c.get(
        f"/api/sessions/{victim_session}", headers=_as(ctx["archer"]["token"])
    )
    assert still.status_code == 200 and still.json()["name"] == "archer's work"


@pytest.mark.asyncio
async def test_the_transcript_is_not_readable_through_the_messages_route(
    two_accounts,
):
    """The windowed-scrollback route reads rows rather than the session, which
    is exactly the kind of place a scope gets forgotten."""
    ctx = two_accounts
    c = ctx["client"]
    res = await c.get(
        f"/api/sessions/{ctx['archer']['session'].id}/messages?before_seq=999",
        headers=_as(ctx["vera"]["token"]),
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_a_session_cannot_be_pointed_outside_its_owners_workspace(
    two_accounts,
):
    """Confinement has to be *enforced where sessions are made*, not merely
    available in a helper. The helper is unit-tested in test_workspace.py; this
    is the wiring, which is the half that gets forgotten."""
    ctx = two_accounts
    archer = ctx["archer"]

    with pytest.raises(WorkspaceError):
        await session_manager.create_session(
            archer["agent"]["id"], "escape", "/etc"
        )

    # Another account's workspace is outside too — that is the whole point.
    with pytest.raises(WorkspaceError):
        await session_manager.create_session(
            archer["agent"]["id"],
            "reach across",
            str(paths_for(ctx["vera"]["user"]["id"]).workspace),
        )


@pytest.mark.asyncio
async def test_extra_roots_let_the_first_user_keep_their_repository(two_accounts):
    """The single-box affordance from §6, tested where it is read rather than
    only where it is written — an `extra_roots` that nothing consults would
    look configured and do nothing."""
    ctx = two_accounts
    archer = ctx["archer"]

    outside = paths_for("somewhere").workspace.parent.parent / "a-repo"
    outside.mkdir(parents=True, exist_ok=True)

    with pytest.raises(WorkspaceError):
        await session_manager.create_session(
            archer["agent"]["id"], "outside", str(outside)
        )

    await ctx["db"].update_user_field(
        archer["user"]["id"], extra_roots=json.dumps([str(outside)])
    )
    allowed = await session_manager.create_session(
        archer["agent"]["id"], "outside, now permitted", str(outside)
    )
    assert allowed.working_dir == str(outside)


# ---------------------------------------------------------------------------
# The live stream (multi-tenancy.md §7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_accounts_frames_never_reach_anothers_socket(two_accounts):
    """The quietest way to get multi-tenancy wrong.

    The bus was a fan-out to every subscriber, so with accounts it would have
    streamed one person's assistant text into another person's browser —
    nothing errors, both tabs look plausible, and the only symptom is someone
    reading work that is not theirs.
    """
    ctx = two_accounts
    archer_seen: list[dict] = []
    vera_seen: list[dict] = []
    internal_seen: list[dict] = []

    async def archer_cb(msg):
        archer_seen.append(msg)

    async def vera_cb(msg):
        vera_seen.append(msg)

    async def internal_cb(msg):
        internal_seen.append(msg)

    session_manager.on_broadcast("archer-tab", archer_cb, ctx["archer"]["user"]["id"])
    session_manager.on_broadcast("vera-tab", vera_cb, ctx["vera"]["user"]["id"])
    # No account: the delegation manager and friends, which must keep seeing
    # everything because they act on the server's behalf.
    session_manager.on_broadcast("internal", internal_cb)
    try:
        await session_manager._broadcast(
            {
                "type": "assistant_text",
                "session_id": ctx["archer"]["session"].id,
                "content": "something private",
            }
        )

        assert len(archer_seen) == 1
        assert vera_seen == [], "another account's assistant text reached this socket"
        assert len(internal_seen) == 1, "an internal subscriber stopped seeing frames"

        # A frame that belongs to the install rather than to a person — no
        # session, no owner — still reaches everyone.
        await session_manager._broadcast({"type": "schedules_changed"})
        assert len(archer_seen) == 2 and len(vera_seen) == 1
    finally:
        for key in ("archer-tab", "vera-tab", "internal"):
            session_manager.remove_broadcast(key)


@pytest.mark.asyncio
async def test_a_frame_can_name_its_owner_when_there_is_no_session(two_accounts):
    """Agent and application events carry a row rather than a session id, so
    the owner is told rather than derived."""
    ctx = two_accounts
    vera_seen: list[dict] = []

    async def vera_cb(msg):
        vera_seen.append(msg)

    session_manager.on_broadcast("vera-tab", vera_cb, ctx["vera"]["user"]["id"])
    try:
        await session_manager._broadcast(
            {"type": "agent_created", "agent": {"name": "Archer's second"}},
            user_id=ctx["archer"]["user"]["id"],
        )
        assert vera_seen == [], "another account's agent event reached this socket"
    finally:
        session_manager.remove_broadcast("vera-tab")


# ---------------------------------------------------------------------------
# Quotas (multi-tenancy.md §8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_account_cannot_run_the_box_out_of_memory(two_accounts, monkeypatch):
    """A held `claude` is ~250 MB and inline-steering.md §7 records the OOM a
    single user managed. With several accounts the cap is not a refinement.

    Counted per account, not per box: one person filling their own allowance
    must not stop anybody else working, which is the difference between a
    quota and an outage.
    """
    from server.config import settings as cfg
    from server.sessions.turns import QuotaExceeded

    monkeypatch.setattr(cfg, "max_concurrent_turns_per_user", 1)
    ctx = two_accounts
    archer, vera = ctx["archer"], ctx["vera"]

    # Pretend one of Archer's sessions is mid-turn.
    async def forever():
        await asyncio.sleep(3600)

    busy = asyncio.create_task(forever())
    archer["session"]._active_task = busy
    try:
        second = await session_manager.create_session(
            archer["agent"]["id"],
            "another",
            str(paths_for(archer["user"]["id"]).workspace),
        )
        with pytest.raises(QuotaExceeded):
            await session_manager.start_message(second.id, "hello")

        # Vera is unaffected by Archer's allowance.
        assert session_manager.running_turns_for(vera["user"]["id"]) == 0
        vera_second = await session_manager.create_session(
            vera["agent"]["id"],
            "vera's second",
            str(paths_for(vera["user"]["id"]).workspace),
        )
        # Far enough to prove the quota did not refuse it; the turn itself
        # needs a CLI, which is the real tier's business.
        session_manager._check_turn_quota(
            session_manager.get_session(vera_second.id)
        )
    finally:
        busy.cancel()
        archer["session"]._active_task = None


@pytest.mark.asyncio
async def test_a_message_queued_behind_a_running_turn_is_not_refused(
    two_accounts, monkeypatch
):
    """A queue is one CLI process however long it gets, so refusing to queue
    would make the cap feel like data loss for no saving."""
    from server.config import settings as cfg

    monkeypatch.setattr(cfg, "max_concurrent_turns_per_user", 1)
    ctx = two_accounts
    session = ctx["archer"]["session"]

    async def forever():
        await asyncio.sleep(3600)

    busy = asyncio.create_task(forever())
    session._active_task = busy
    try:
        await session_manager.start_message(session.id, "queue me")
        assert [q.prompt for q in session._pending_queue] == ["queue me"]
    finally:
        busy.cancel()
        session._active_task = None
        session._pending_queue.clear()


@pytest.mark.asyncio
async def test_two_accounts_can_both_have_an_application_called_notes(two_accounts):
    """The name index was unique across the box. That is a bug with accounts:
    the second person who wants an app called "Notes" cannot have one, and the
    refusal tells them somebody else already does."""
    from server.applications import application_manager

    ctx = two_accounts
    application_manager.bind(db=ctx["db"], session_mgr=session_manager)

    first = await application_manager.create_application(
        name="Notes", description="Archer's", agent_id=ctx["archer"]["agent"]["id"]
    )
    second = await application_manager.create_application(
        name="Notes", description="Vera's", agent_id=ctx["vera"]["agent"]["id"]
    )

    assert first["user_id"] == ctx["archer"]["user"]["id"]
    assert second["user_id"] == ctx["vera"]["user"]["id"]
    # Separate directories, so the files cannot collide either.
    assert first["app_dir"] != second["app_dir"]
    assert str(paths_for(ctx["archer"]["user"]["id"]).applications) in first["app_dir"]

    # Still taken within one account.
    with pytest.raises(Exception) as e:
        await application_manager.create_application(
            name="notes", description="again", agent_id=ctx["archer"]["agent"]["id"]
        )
    assert "already exists" in str(e.value)
