"""Tests for agent-to-agent delegation (agent-collaboration.md Phase 1).

Covers:
  * Schema migration adds `parent_session_id` (FK SET NULL on parent
    delete) and `delegation_request`; `origin='delegation'` round-trips.
  * `DelegationManager` agent-name resolution: case-insensitive,
    missing → 404 (None), ambiguous → 409, self-delegation → 409.
  * Cycle + depth guards on the parent chain.
  * Reply / error / cancellation injection paths produce the right
    structured prompt back into the parent session via
    `SessionManager.start_message`.
  * Multiple concurrent delegations under the same target agent.
  * Bridge broadcast scoping skips delegation children automatically
    (no chat ever maps to a delegation session id).
  * The REST routes (POST/GET/cancel under
    `/api/sessions/{sid}/delegations`).

We use real DelegationManager instances bound to per-test in-memory
SessionManagers. The parent session's `start_message` is patched in
the broadcast-injection tests so we never spawn a real harness — the
captured prompts are what matters.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from server.agent_manager import AgentManager
from server.database import Database
from server.delegations import (
    DEPTH_CAP,
    DelegationError,
    DelegationManager,
    delegation_manager as singleton_delegation_manager,
)
from server.main import app
from server.routers import agents as agents_mod
from server.routers import schedules as schedules_mod
from server.scheduler import ScheduleRunner
from server.session_manager import session_manager

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
async def mgr(db):
    """Per-test SessionManager bound to a per-test DB."""
    from server.session_manager import SessionManager

    m = SessionManager()
    await m.initialize(db)
    yield m


@pytest.fixture
async def dm(mgr, db):
    """Per-test DelegationManager bound to the test session manager.
    A fresh instance every test so registries don't leak across cases."""
    dm = DelegationManager()
    dm.bind(session_mgr=mgr, db=db)
    yield dm
    dm.shutdown()


async def _make_agent(db, name: str) -> dict:
    am = AgentManager(db)
    return await am.create_agent(name=name)


async def _make_session(
    mgr, agent_id: str, *, name: str = "S", origin: str = "user",
    parent_session_id: str | None = None,
):
    return await mgr.create_session(
        agent_id=agent_id,
        name=name,
        working_dir="/tmp",
        origin=origin,
        parent_session_id=parent_session_id,
    )


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_has_new_columns(db):
    cols = {row[1] for row in await db._column_info("sessions")}
    assert "parent_session_id" in cols
    assert "delegation_request" in cols


@pytest.mark.asyncio
async def test_origin_delegation_round_trips(mgr, db):
    parent_agent = await db.get_system_agent()
    child_agent = await _make_agent(db, "Vera")
    parent = await _make_session(mgr, parent_agent["id"], name="parent")
    child = await mgr.create_session(
        agent_id=child_agent["id"],
        name="vera-child",
        working_dir="/tmp",
        origin="delegation",
        parent_session_id=parent.id,
        delegation_request="please review",
    )
    assert child.origin == "delegation"
    assert child.parent_session_id == parent.id
    assert child.delegation_request == "please review"

    # Persists across a reload from the DB.
    rows = await db.load_sessions()
    raw = next(r for r in rows if r["id"] == child.id)
    assert raw["origin"] == "delegation"
    assert raw["parent_session_id"] == parent.id
    assert raw["delegation_request"] == "please review"


@pytest.mark.asyncio
async def test_parent_delete_sets_null_on_child(mgr, db):
    parent_agent = await db.get_system_agent()
    child_agent = await _make_agent(db, "Vera")
    parent = await _make_session(mgr, parent_agent["id"])
    child = await mgr.create_session(
        agent_id=child_agent["id"],
        name="c",
        working_dir="/tmp",
        origin="delegation",
        parent_session_id=parent.id,
        delegation_request="r",
    )
    await db.delete_session(parent.id)
    rows = await db.load_sessions()
    raw = next(r for r in rows if r["id"] == child.id)
    assert raw["parent_session_id"] is None
    # The child itself survives — orphaning beats mass-delete.
    assert raw["id"] == child.id


# ---------------------------------------------------------------------------
# Agent resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_target_agent_case_insensitive(dm, db):
    await _make_agent(db, "Vera")
    assert (await dm._resolve_target_agent("vera"))["name"] == "Vera"
    assert (await dm._resolve_target_agent("VERA"))["name"] == "Vera"
    assert (await dm._resolve_target_agent("  Vera  "))["name"] == "Vera"


@pytest.mark.asyncio
async def test_resolve_target_agent_missing_returns_none(dm, db):
    assert await dm._resolve_target_agent("nobody") is None


# ---------------------------------------------------------------------------
# start_delegation: end-to-end happy path + guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_delegation_happy_path(dm, mgr, db, monkeypatch):
    # Disable real harness spawn — we only want to verify the wiring.
    started: list[tuple[str, str]] = []

    async def fake_start_message(sid, prompt, attachment_ids=None):
        started.append((sid, prompt))

    monkeypatch.setattr(mgr, "start_message", fake_start_message)

    parent_agent = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, parent_agent["id"], name="parent")

    rec = await dm.start_delegation(
        parent_session_id=parent.id,
        agent_name="vera",
        request="review the dashboard",
        files=["web/src/Dashboard.tsx"],
    )
    assert rec.state == "running"
    assert rec.delegation_id != parent.id
    assert rec.target_agent_name == "Vera"
    assert rec.parent_session_id == parent.id

    # A child session row exists with origin=delegation, parent set.
    child = mgr.get_session(rec.delegation_id)
    assert child is not None
    assert child.origin == "delegation"
    assert child.parent_session_id == parent.id
    assert child.delegation_request == "review the dashboard"
    assert child.working_dir == parent.working_dir  # inherited

    # The child's first turn was kicked off — the composed prompt
    # mentions the parent agent and the request, and lists files.
    assert started, "expected start_message to be called on the child"
    sid, prompt = started[0]
    assert sid == rec.delegation_id
    assert "Octo" in prompt  # default system agent's name
    assert "review the dashboard" in prompt
    assert "web/src/Dashboard.tsx" in prompt


@pytest.mark.asyncio
async def test_start_delegation_rejects_empty_request(dm, mgr, db, monkeypatch):
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    await _make_agent(db, "Vera")
    parent_agent = await db.get_system_agent()
    parent = await _make_session(mgr, parent_agent["id"])
    with pytest.raises(DelegationError) as excinfo:
        await dm.start_delegation(
            parent_session_id=parent.id,
            agent_name="vera",
            request="   ",
        )
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_start_delegation_unknown_parent(dm):
    with pytest.raises(DelegationError) as excinfo:
        await dm.start_delegation(
            parent_session_id="nonexistent",
            agent_name="vera",
            request="hi",
        )
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_start_delegation_unknown_target(dm, mgr, db, monkeypatch):
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    parent_agent = await db.get_system_agent()
    parent = await _make_session(mgr, parent_agent["id"])
    with pytest.raises(DelegationError) as excinfo:
        await dm.start_delegation(
            parent_session_id=parent.id,
            agent_name="ghost",
            request="hi",
        )
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_start_delegation_rejects_self(dm, mgr, db, monkeypatch):
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    parent_agent = await db.get_system_agent()
    parent = await _make_session(mgr, parent_agent["id"])
    with pytest.raises(DelegationError) as excinfo:
        await dm.start_delegation(
            parent_session_id=parent.id,
            agent_name=parent_agent["name"],
            request="hi",
        )
    assert excinfo.value.status_code == 409
    assert "yourself" in str(excinfo.value)


@pytest.mark.asyncio
async def test_cycle_rejected(dm, mgr, db, monkeypatch):
    """Octo → Vera → Octo is rejected at the cycle check."""
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    vera = await _make_agent(db, "Vera")

    # Octo's user session
    octo_sess = await _make_session(mgr, octo["id"], name="octo-user")
    # Vera child under Octo
    vera_sess = await mgr.create_session(
        agent_id=vera["id"],
        name="vera",
        working_dir="/tmp",
        origin="delegation",
        parent_session_id=octo_sess.id,
        delegation_request="r",
    )
    # Now Vera tries to ask Octo — cycle.
    with pytest.raises(DelegationError) as excinfo:
        await dm.start_delegation(
            parent_session_id=vera_sess.id,
            agent_name=octo["name"],
            request="back to you",
        )
    assert excinfo.value.status_code == 409
    assert "Cycle" in str(excinfo.value)


@pytest.mark.asyncio
async def test_depth_cap_rejected(dm, mgr, db, monkeypatch):
    """A 4th delegation hop is rejected (DEPTH_CAP=3)."""
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    a = await _make_agent(db, "A")
    b = await _make_agent(db, "B")
    c = await _make_agent(db, "C")
    d = await _make_agent(db, "D")

    root = await _make_session(mgr, octo["id"], name="root")
    s_a = await mgr.create_session(
        agent_id=a["id"], name="a", working_dir="/tmp",
        origin="delegation", parent_session_id=root.id, delegation_request="r",
    )
    s_b = await mgr.create_session(
        agent_id=b["id"], name="b", working_dir="/tmp",
        origin="delegation", parent_session_id=s_a.id, delegation_request="r",
    )
    s_c = await mgr.create_session(
        agent_id=c["id"], name="c", working_dir="/tmp",
        origin="delegation", parent_session_id=s_b.id, delegation_request="r",
    )
    # Chain so far: 3 delegation hops (A, B, C). Adding D would make 4.
    with pytest.raises(DelegationError) as excinfo:
        await dm.start_delegation(
            parent_session_id=s_c.id, agent_name=d["name"], request="r",
        )
    assert excinfo.value.status_code == 409
    assert str(DEPTH_CAP) in str(excinfo.value)


# ---------------------------------------------------------------------------
# Broadcast → injection
# ---------------------------------------------------------------------------


async def _noop_start_message(sid, prompt, attachment_ids=None):
    return None


@pytest.mark.asyncio
async def test_reply_injection_on_result(dm, mgr, db, monkeypatch):
    """assistant_text + result(is_error=False) → [agent-reply:...] turn."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None):
        injected.append((sid, prompt))

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"], name="parent")
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    # First call was the child's start_message; clear it so we only
    # see the injection.
    injected.clear()

    cid = rec.delegation_id
    await dm._on_broadcast({"type": "assistant_text", "session_id": cid, "content": "Reviewed. "})
    await dm._on_broadcast({"type": "assistant_text", "session_id": cid, "content": "Looks good."})
    await dm._on_broadcast({"type": "result", "session_id": cid, "is_error": False})

    assert rec.state == "completed"
    assert rec.finished_at is not None
    assert len(injected) == 1
    target_sid, prompt = injected[0]
    assert target_sid == parent.id
    assert prompt.startswith(f"[agent-reply:Vera delegation={cid}]")
    assert "Reviewed. Looks good." in prompt


@pytest.mark.asyncio
async def test_error_injection_on_result_error(dm, mgr, db, monkeypatch):
    """result(is_error=True) → [agent-error:...] turn, state=failed."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None):
        injected.append((sid, prompt))

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    injected.clear()

    cid = rec.delegation_id
    await dm._on_broadcast({"type": "result", "session_id": cid, "is_error": True})

    assert rec.state == "failed"
    assert len(injected) == 1
    _, prompt = injected[0]
    assert prompt.startswith(f"[agent-error:Vera delegation={cid}")


@pytest.mark.asyncio
async def test_error_injection_on_error_event(dm, mgr, db, monkeypatch):
    """An explicit error event from the child also terminates."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None):
        injected.append((sid, prompt))

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    injected.clear()

    cid = rec.delegation_id
    await dm._on_broadcast({
        "type": "error", "session_id": cid, "message": "harness died",
    })

    assert rec.state == "failed"
    assert "harness died" in rec.error
    assert len(injected) == 1


@pytest.mark.asyncio
async def test_post_terminal_events_ignored(dm, mgr, db, monkeypatch):
    """Once a delegation is terminal, late events don't reopen it."""
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    cid = rec.delegation_id
    await dm._on_broadcast({"type": "result", "session_id": cid, "is_error": False})
    state_before = rec.state
    await dm._on_broadcast({"type": "assistant_text", "session_id": cid, "content": "late"})
    await dm._on_broadcast({"type": "result", "session_id": cid, "is_error": True})
    assert rec.state == state_before


@pytest.mark.asyncio
async def test_empty_reply_gets_placeholder(dm, mgr, db, monkeypatch):
    """A child that ends successfully without any assistant_text still
    gets a reply injection, with a placeholder body."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None):
        injected.append((sid, prompt))

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    injected.clear()
    cid = rec.delegation_id
    await dm._on_broadcast({"type": "result", "session_id": cid, "is_error": False})
    _, prompt = injected[0]
    assert "without producing any text" in prompt


# ---------------------------------------------------------------------------
# Cancellation, listing, concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_delegation(dm, mgr, db, monkeypatch):
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None):
        injected.append((sid, prompt))

    async def fake_interrupt(sid):
        return True

    monkeypatch.setattr(mgr, "start_message", capture)
    monkeypatch.setattr(mgr, "interrupt", fake_interrupt)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    injected.clear()
    updated = await dm.cancel_delegation(rec.delegation_id, reason="user clicked stop")
    assert updated.state == "cancelled"
    assert len(injected) == 1
    _, prompt = injected[0]
    assert "agent-error:Vera" in prompt
    assert "user clicked stop" in prompt
    # Idempotent.
    again = await dm.cancel_delegation(rec.delegation_id)
    assert again.state == "cancelled"


@pytest.mark.asyncio
async def test_list_delegations_newest_first(dm, mgr, db, monkeypatch):
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    await _make_agent(db, "Pete")
    parent = await _make_session(mgr, octo["id"])
    r1 = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r1",
    )
    r2 = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="pete", request="r2",
    )
    listed = dm.list_delegations(parent.id)
    assert [r.delegation_id for r in listed] == [r2.delegation_id, r1.delegation_id]


@pytest.mark.asyncio
async def test_concurrent_delegations_to_same_target(dm, mgr, db, monkeypatch):
    """Two delegations to Vera in flight at once: both run, both reply
    independently, parent sees both terminal turns."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None):
        injected.append((sid, prompt))

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])

    rec1 = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r1",
    )
    rec2 = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r2",
    )
    assert rec1.delegation_id != rec2.delegation_id
    injected.clear()

    # Send each child its own result.
    await dm._on_broadcast({"type": "assistant_text", "session_id": rec1.delegation_id, "content": "one"})
    await dm._on_broadcast({"type": "result", "session_id": rec1.delegation_id, "is_error": False})
    await dm._on_broadcast({"type": "assistant_text", "session_id": rec2.delegation_id, "content": "two"})
    await dm._on_broadcast({"type": "result", "session_id": rec2.delegation_id, "is_error": False})

    assert rec1.state == "completed"
    assert rec2.state == "completed"
    # Two terminal injections into the same parent, ordered by completion.
    assert len(injected) == 2
    assert all(t[0] == parent.id for t in injected)
    assert f"delegation={rec1.delegation_id}" in injected[0][1]
    assert f"delegation={rec2.delegation_id}" in injected[1][1]


# ---------------------------------------------------------------------------
# Bridges don't fan out delegation sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_manager_skips_delegation_session(mgr, db, monkeypatch):
    """The bridge broadcast filter is session-id-scoped: a chat is bound
    to one sticky session. A delegation session has no bridge binding,
    so events from it never reach any chat. We verify by sending a
    broadcast for a delegation session id and confirming no bridge
    bridge.handle_event call was attempted."""
    from server.bridges.manager import BridgeManager

    bm = BridgeManager(mgr, db)

    # A chat bound to Vera but to the user-origin session of Vera, NOT
    # to a delegation child of Vera.
    octo = await db.get_system_agent()
    vera = await _make_agent(db, "Vera")
    vera_user_sess = await _make_session(mgr, vera["id"], name="vera-user")
    # Hand-register a binding to that user session id.
    from server.bridges.manager import ChatBinding

    bm._mappings["telegram:42"] = ChatBinding(
        agent_id=vera["id"], session_id=vera_user_sess.id, verbose=False
    )

    # Create a delegation child under Vera (parent = an Octo session).
    octo_sess = await _make_session(mgr, octo["id"])
    delegation_child = await mgr.create_session(
        agent_id=vera["id"], name="d", working_dir="/tmp",
        origin="delegation", parent_session_id=octo_sess.id,
        delegation_request="r",
    )

    # Pretend the harness broadcast an event for the delegation child.
    # Patch all bridge handle_event calls so we'd see any leak.
    leaked: list[tuple[str, dict]] = []

    class FakeBridge:
        async def handle_event(self, chat_id, msg):
            leaked.append((chat_id, msg))

    bm._bridges["telegram"] = FakeBridge()

    await bm._on_broadcast({
        "type": "assistant_text", "session_id": delegation_child.id,
        "content": "leak?",
    })
    assert leaked == []


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(db):
    """HTTP test client with the module-level singletons rebound to the
    per-test in-memory DB. Bridges/scheduler are stood up so app
    lifespan doesn't matter for these tests."""
    session_manager.sessions.clear()
    await session_manager.initialize(db)

    agents_mod.set_manager(AgentManager(db))
    runner = ScheduleRunner(session_manager, db)
    await runner.initialize()
    schedules_mod._db = db
    schedules_mod._runner = runner

    singleton_delegation_manager.bind(session_mgr=session_manager, db=db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    singleton_delegation_manager.shutdown()
    # Drain the registry between tests so list_delegations doesn't leak
    # state into the next case.
    singleton_delegation_manager._records.clear()
    await runner.shutdown()


async def _post_agent(client, name, **extra):
    r = await client.post(
        "/api/agents", json={"name": name, **extra}, headers=HEADERS
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _post_session(client, agent_id, name="parent"):
    r = await client.post(
        "/api/sessions",
        json={"name": name, "working_dir": "/tmp", "agent_id": agent_id},
        headers=HEADERS,
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_route_start_delegation(client, monkeypatch):
    """POST /sessions/{sid}/delegations succeeds; child session shows
    up via GET /sessions/{child_sid}."""
    monkeypatch.setattr(session_manager, "start_message", _noop_start_message)

    agents = (await client.get("/api/agents", headers=HEADERS)).json()
    octo = next(a for a in agents if a["is_system"])
    await _post_agent(client, "Vera")
    parent = await _post_session(client, octo["id"])

    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations",
        json={"agent_name": "vera", "request": "review the dashboard"},
        headers=HEADERS,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["state"] == "running"
    assert body["target_agent_name"] == "Vera"
    assert body["parent_session_id"] == parent["id"]
    assert body["delegation_id"] == body["sub_session_id"]

    # Child session is visible via the normal GET route.
    cid = body["delegation_id"]
    detail = (await client.get(f"/api/sessions/{cid}", headers=HEADERS)).json()
    assert detail["origin"] == "delegation"
    assert detail["parent_session_id"] == parent["id"]
    assert detail["delegation_request"] == "review the dashboard"


@pytest.mark.asyncio
async def test_route_list_and_cancel(client, monkeypatch):
    monkeypatch.setattr(session_manager, "start_message", _noop_start_message)

    async def fake_interrupt(sid):
        return True

    monkeypatch.setattr(session_manager, "interrupt", fake_interrupt)

    agents = (await client.get("/api/agents", headers=HEADERS)).json()
    octo = next(a for a in agents if a["is_system"])
    await _post_agent(client, "Vera")
    parent = await _post_session(client, octo["id"])

    create = await client.post(
        f"/api/sessions/{parent['id']}/delegations",
        json={"agent_name": "vera", "request": "go"},
        headers=HEADERS,
    )
    did = create.json()["delegation_id"]

    listing = await client.get(
        f"/api/sessions/{parent['id']}/delegations", headers=HEADERS
    )
    assert listing.status_code == 200
    rows = listing.json()
    assert [r["delegation_id"] for r in rows] == [did]

    cancel = await client.post(
        f"/api/sessions/{parent['id']}/delegations/{did}/cancel",
        json={"reason": "test"},
        headers=HEADERS,
    )
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["state"] == "cancelled"


@pytest.mark.asyncio
async def test_route_404s(client, monkeypatch):
    monkeypatch.setattr(session_manager, "start_message", _noop_start_message)
    # Unknown parent session.
    r = await client.post(
        "/api/sessions/ghost/delegations",
        json={"agent_name": "vera", "request": "go"},
        headers=HEADERS,
    )
    assert r.status_code == 404

    # Known parent, unknown agent.
    agents = (await client.get("/api/agents", headers=HEADERS)).json()
    octo = next(a for a in agents if a["is_system"])
    parent = await _post_session(client, octo["id"])
    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations",
        json={"agent_name": "ghost", "request": "go"},
        headers=HEADERS,
    )
    assert r.status_code == 404

    # Cancel an unknown delegation.
    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations/nope/cancel",
        json={},
        headers=HEADERS,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_route_self_delegation_409(client, monkeypatch):
    monkeypatch.setattr(session_manager, "start_message", _noop_start_message)
    agents = (await client.get("/api/agents", headers=HEADERS)).json()
    octo = next(a for a in agents if a["is_system"])
    parent = await _post_session(client, octo["id"])
    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations",
        json={"agent_name": octo["name"], "request": "x"},
        headers=HEADERS,
    )
    assert r.status_code == 409
