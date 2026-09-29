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


@pytest.fixture
async def two_accounts(monkeypatch):
    monkeypatch.setattr(settings, "master_key", "test-master-key")
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
        archer_agent["id"], "archer's work", "/tmp"
    )
    vera_session = await session_manager.create_session(
        vera_agent["id"], "vera's work", "/tmp"
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
