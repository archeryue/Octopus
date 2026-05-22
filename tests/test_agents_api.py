"""Tests for the /api/agents routes (agent-refactor.md §5.4 / §8)."""

import pytest
from httpx import ASGITransport, AsyncClient

from server.agent_manager import AgentManager
from server.database import Database
from server.main import app
from server.routers import agents as agents_mod
from server.routers import schedules as schedules_mod
from server.scheduler import ScheduleRunner
from server.session_manager import session_manager

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def client():
    db = Database(":memory:")
    await db.initialize()
    session_manager.sessions.clear()
    await session_manager.initialize(db)

    agents_mod.set_manager(AgentManager(db))
    runner = ScheduleRunner(session_manager, db)
    await runner.initialize()
    schedules_mod._db = db
    schedules_mod._runner = runner

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await runner.shutdown()
    await db.close()


async def _create_agent(client, name="Researcher", **extra):
    body = {"name": name, **extra}
    resp = await client.post("/api/agents", json=body, headers=HEADERS)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- Default Agent ---


@pytest.mark.asyncio
async def test_default_agent_present(client):
    resp = await client.get("/api/agents", headers=HEADERS)
    assert resp.status_code == 200
    agents = resp.json()
    system = [a for a in agents if a["is_system"]]
    assert len(system) == 1
    assert system[0]["name"] == "Octo"
    assert system[0]["mcp_servers"] == ["ask", "bg", "viewer"]


@pytest.mark.asyncio
async def test_auth_required(client):
    resp = await client.get("/api/agents")
    assert resp.status_code in (401, 403)


# --- CRUD ---


@pytest.mark.asyncio
async def test_create_and_get_agent(client):
    agent = await _create_agent(
        client,
        name="Inbox Bot",
        system_prompt="You triage email.",
        model="claude-opus-4-7",
        tool_allow="Read\nGrep",
        tool_deny="Bash",
    )
    assert agent["name"] == "Inbox Bot"
    assert agent["system_prompt"] == "You triage email."
    assert agent["model"] == "claude-opus-4-7"
    assert agent["tool_allow"] == "Read\nGrep"
    assert agent["tool_deny"] == "Bash"
    assert agent["is_system"] is False
    assert agent["active_session_count"] == 0

    got = await client.get(f"/api/agents/{agent['id']}", headers=HEADERS)
    assert got.status_code == 200
    assert got.json()["id"] == agent["id"]


@pytest.mark.asyncio
async def test_get_unknown_agent_404(client):
    resp = await client.get("/api/agents/nope", headers=HEADERS)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_duplicate_name_rejected(client):
    await _create_agent(client, name="Dup")
    resp = await client.post("/api/agents", json={"name": "Dup"}, headers=HEADERS)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_patch_agent(client):
    agent = await _create_agent(client, name="Patchable", model="claude-opus-4-7")
    resp = await client.patch(
        f"/api/agents/{agent['id']}",
        json={"system_prompt": "new prompt", "model": None},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["system_prompt"] == "new prompt"
    # Explicit null clears the nullable model field.
    assert data["model"] is None


@pytest.mark.asyncio
async def test_patch_duplicate_name_rejected(client):
    await _create_agent(client, name="First")
    second = await _create_agent(client, name="Second")
    resp = await client.patch(
        f"/api/agents/{second['id']}", json={"name": "First"}, headers=HEADERS
    )
    assert resp.status_code == 400


# --- is_system protection ---


@pytest.mark.asyncio
async def test_default_agent_cannot_be_archived_or_deleted(client):
    agents = (await client.get("/api/agents", headers=HEADERS)).json()
    default = next(a for a in agents if a["is_system"])
    arch = await client.post(f"/api/agents/{default['id']}/archive", headers=HEADERS)
    assert arch.status_code == 400
    dele = await client.delete(f"/api/agents/{default['id']}", headers=HEADERS)
    assert dele.status_code == 400


# --- delete / archive ---


@pytest.mark.asyncio
async def test_delete_empty_agent(client):
    agent = await _create_agent(client, name="Throwaway")
    resp = await client.delete(f"/api/agents/{agent['id']}", headers=HEADERS)
    assert resp.status_code == 204
    assert (await client.get(f"/api/agents/{agent['id']}", headers=HEADERS)).status_code == 404


@pytest.mark.asyncio
async def test_delete_agent_with_sessions_refused(client):
    agent = await _create_agent(client, name="Busy")
    await client.post(
        f"/api/agents/{agent['id']}/sessions", json={"name": "s"}, headers=HEADERS
    )
    resp = await client.delete(f"/api/agents/{agent['id']}", headers=HEADERS)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_archive_agent_cascades_sessions(client):
    agent = await _create_agent(client, name="Archivable")
    sess = (
        await client.post(
            f"/api/agents/{agent['id']}/sessions", json={"name": "s"}, headers=HEADERS
        )
    ).json()
    resp = await client.post(f"/api/agents/{agent['id']}/archive", headers=HEADERS)
    assert resp.status_code == 200

    # Its session is no longer in the live list.
    live = (await client.get("/api/sessions", headers=HEADERS)).json()
    assert sess["id"] not in [s["id"] for s in live]
    # But it's there as archived.
    arch = (
        await client.get("/api/sessions?include_archived=true", headers=HEADERS)
    ).json()
    assert sess["id"] in [s["id"] for s in arch]


# --- agent-scoped sessions & schedules ---


@pytest.mark.asyncio
async def test_create_session_under_agent_inherits_agent_id(client):
    agent = await _create_agent(client, name="Owner")
    resp = await client.post(
        f"/api/agents/{agent['id']}/sessions", json={"name": "thread"}, headers=HEADERS
    )
    assert resp.status_code == 201
    session = resp.json()
    assert session["agent_id"] == agent["id"]
    assert session["origin"] == "user"

    listed = await client.get(f"/api/agents/{agent['id']}/sessions", headers=HEADERS)
    assert [s["id"] for s in listed.json()] == [session["id"]]


@pytest.mark.asyncio
async def test_session_create_defaults_to_default_agent(client):
    """POST /api/sessions without agent_id falls back to the Default Agent
    (one-release compat)."""
    resp = await client.post("/api/sessions", json={"name": "x"}, headers=HEADERS)
    assert resp.status_code == 201
    agents = (await client.get("/api/agents", headers=HEADERS)).json()
    default = next(a for a in agents if a["is_system"])
    assert resp.json()["agent_id"] == default["id"]


@pytest.mark.asyncio
async def test_session_create_unknown_agent_400(client):
    resp = await client.post(
        "/api/sessions", json={"name": "x", "agent_id": "ghost"}, headers=HEADERS
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_agent_scoped_schedule_crud(client):
    agent = await _create_agent(client, name="Scheduled")
    resp = await client.post(
        f"/api/agents/{agent['id']}/schedules",
        json={"name": "daily", "prompt": "summarize", "interval_seconds": 60},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    sched = resp.json()
    assert sched["agent_id"] == agent["id"]

    listed = await client.get(f"/api/agents/{agent['id']}/schedules", headers=HEADERS)
    assert [s["id"] for s in listed.json()] == [sched["id"]]


# --- natural-language /schedule (from_text) ---


@pytest.mark.asyncio
async def test_schedule_from_text_rigid(client):
    """Explicit "<interval> <prompt>" needs no AI — the rigid fast path runs."""
    agent = await _create_agent(client, name="Rigid Sched")
    resp = await client.post(
        f"/api/agents/{agent['id']}/schedules/from_text",
        json={"text": "30m check the build"},
        headers=HEADERS,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["interval_seconds"] == 1800
    assert data["cron"] is None
    assert data["prompt"] == "check the build"
    assert data["recurrence_label"] == "Every 30m"


@pytest.mark.asyncio
async def test_schedule_from_text_ai_cron(client, monkeypatch):
    """Natural language → AI parse → cron schedule (AI call mocked)."""
    from server import schedule_ai

    async def fake_oneshot(prompt, **kwargs):
        assert "America/Los_Angeles" in prompt
        return (
            '{"name":"Gmail summary","prompt":"summarize my unread email",'
            '"recurrence":{"kind":"cron","cron":"0 9 * * *"},'
            '"recurrence_label":"Every day at 9:00 AM"}'
        )

    monkeypatch.setattr(schedule_ai, "run_claude_oneshot", fake_oneshot)

    agent = await _create_agent(client, name="NL Sched")
    resp = await client.post(
        f"/api/agents/{agent['id']}/schedules/from_text",
        json={
            "text": "summarize my unread email every morning at 9am",
            "timezone": "America/Los_Angeles",
            "session_id": "chat-session-1",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["cron"] == "0 9 * * *"
    assert data["timezone"] == "America/Los_Angeles"
    assert data["interval_seconds"] is None
    assert data["recurrence_label"] == "Every day at 9:00 AM"
    assert data["prompt"] == "summarize my unread email"
    # The session the command was issued from is remembered so each fire
    # appends the summary into that same conversation.
    assert data["origin_session_id"] == "chat-session-1"


@pytest.mark.asyncio
async def test_schedule_from_text_parse_error_is_422(client, monkeypatch):
    from server import schedule_ai

    async def fake_oneshot(prompt, **kwargs):
        return "I'm not sure what you mean."

    monkeypatch.setattr(schedule_ai, "run_claude_oneshot", fake_oneshot)

    agent = await _create_agent(client, name="Bad Sched")
    resp = await client.post(
        f"/api/agents/{agent['id']}/schedules/from_text",
        json={"text": "do something clever at some point"},
        headers=HEADERS,
    )
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)


@pytest.mark.asyncio
async def test_schedule_from_text_unknown_agent(client):
    resp = await client.post(
        "/api/agents/nope/schedules/from_text",
        json={"text": "30m check the build"},
        headers=HEADERS,
    )
    assert resp.status_code == 404
