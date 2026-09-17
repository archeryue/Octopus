"""Applications talking to agents (docs/plans/app-agent-access.md).

Covers the manager (conversations as sessions, the prompt the agent reads,
every cap and every refusal) and the HTTP surface an app actually calls,
including the scoped token a backend script is given — which must open exactly
one application and nothing else.

No harness ever spawns: `start_message` is replaced with a fake that plays the
broadcast events a real turn would emit, which is also what makes the event
vocabulary assertable.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os

import pytest
from httpx import ASGITransport, AsyncClient

from server import app_agent as app_agent_mod
from server.agent_manager import AgentManager
from server.app_agent import (
    AppAgentError,
    AppAgentManager,
    ORIGIN_APP,
)
from server.app_backends import script_env
from server.applications import (
    ApplicationManager,
    app_scope_token,
    application_manager as singleton_application_manager,
    data_dir_for,
    is_app_scope_token,
)
from server.config import settings
from server.database import Database
from server.main import app
from server.models import MessageContent
from server.routers import agents as agents_mod
from server.routers import applications as applications_mod
from server.session_manager import SessionManager, session_manager

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
# How the page inside the iframe authenticates: the app cookie the SPA sets
# before mounting the frame. Sent as a raw header so httpx keeps it per
# request rather than in a shared jar.
APP_COOKIE = {"Cookie": f"{applications_mod.APP_TOKEN_COOKIE}={TOKEN}"}


# --------------------------------------------------------------- fixtures


@pytest.fixture
def apps_root(tmp_path, monkeypatch):
    root = tmp_path / "applications"
    monkeypatch.setattr(settings, "applications_dir", str(root))
    return root


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
async def mgr(db):
    m = SessionManager()
    await m.initialize(db)
    yield m


@pytest.fixture
async def am(mgr, db, apps_root):
    m = ApplicationManager()
    m.bind(session_mgr=mgr, db=db)
    yield m
    m.shutdown()


@pytest.fixture
async def aam(mgr, db, am):
    m = AppAgentManager()
    m.bind(session_mgr=mgr, db=db, app_mgr=am)
    yield m
    m.shutdown()


class FakeTurns:
    """Stands in for the harness: records prompts and plays back the events a
    turn would broadcast. `script` is what the agent "says"."""

    def __init__(self, mgr):
        self.mgr = mgr
        self.prompts: list[tuple[str, str]] = []
        self.reply = "Sure — the article argues three things."
        self.is_error = False
        self.emit = True

    async def start_message(self, session_id, prompt, attachment_ids=None, **kw):
        self.prompts.append((session_id, prompt))
        if not self.emit:
            return
        for chunk in ("Sure — ", "the article "):
            await self.mgr._broadcast(
                {"type": "assistant_delta", "session_id": session_id, "content": chunk}
            )
        await self.mgr._broadcast(
            {"type": "tool_use", "session_id": session_id, "tool": "WebFetch",
             "input": {"url": "http://x"}, "tool_use_id": "t1"}
        )
        if not self.is_error:
            await self.mgr._broadcast(
                {"type": "assistant_text", "session_id": session_id,
                 "content": self.reply}
            )
        await self.mgr._broadcast(
            {"type": "result", "session_id": session_id, "is_error": self.is_error,
             "cost": 0.004}
        )


@pytest.fixture
def turns(mgr, monkeypatch):
    fake = FakeTurns(mgr)
    monkeypatch.setattr(mgr, "start_message", fake.start_message)
    return fake


async def _make_app(am, db, *, name="SmartReader") -> dict:
    agent = await AgentManager(db).create_agent(name=f"{name} Agent")
    return await am.create_application(
        name=name,
        description="Reads a page and talks about it",
        agent_id=agent["id"],
    )


# ------------------------------------------------------------ the token


def test_scope_token_is_per_app_and_not_the_master_token():
    a, b = app_scope_token("app-a"), app_scope_token("app-b")
    assert a != b
    assert a != settings.auth_token
    assert is_app_scope_token("app-a", a)
    # The whole point: A's token proves nothing about B.
    assert not is_app_scope_token("app-b", a)
    assert not is_app_scope_token("app-a", "")
    assert not is_app_scope_token("app-a", None)


def test_scope_token_follows_the_master_token(monkeypatch):
    before = app_scope_token("app-a")
    monkeypatch.setattr(settings, "auth_token", "rotated-secret")
    assert app_scope_token("app-a") != before


def test_backend_scripts_get_the_agent_api_but_never_the_master_token():
    env = script_env("app-a", "/tmp/app-a")
    assert env["OCTOPUS_AGENT_API"].endswith("/apps/app-a/agent")
    assert env["OCTOPUS_APP_TOKEN"] == app_scope_token("app-a")
    assert settings.auth_token not in env.values()


# ------------------------------------------------------- the conversation


@pytest.mark.asyncio
async def test_ask_opens_a_conversation_and_returns_the_reply(aam, am, db, turns):
    row = await _make_app(am, db)
    turns.prompts.clear()

    out = await aam.ask(row, message="What does it say?")

    assert out["reply"] == turns.reply
    assert out["cost"] == 0.004
    session = aam.session_mgr.get_session(out["conversation_id"])
    assert session.origin == ORIGIN_APP
    assert session.app_id == row["id"]
    # Files the agent writes belong to the app's state, not its source.
    assert session.working_dir == data_dir_for(row["app_dir"])
    assert os.path.isdir(session.working_dir)


@pytest.mark.asyncio
async def test_the_first_turn_carries_the_preamble_and_later_ones_do_not(
    aam, am, db, turns
):
    row = await _make_app(am, db)
    turns.prompts.clear()

    first = await aam.ask(row, message="What does it say?", context="ARTICLE BODY")
    _, prompt = turns.prompts[-1]
    assert 'inside "SmartReader"' in prompt
    assert "Do NOT edit this application's code" in prompt
    assert "[app:SmartReader]" in prompt
    # Context is labelled as material, not as the person talking.
    assert "--- context from the application ---\nARTICLE BODY" in prompt

    await aam.ask(
        row, message="And the second point?", conversation_id=first["conversation_id"]
    )
    sid, second = turns.prompts[-1]
    assert sid == first["conversation_id"]
    assert "inside \"SmartReader\"" not in second
    assert "[app:SmartReader]\nAnd the second point?" in second
    # One session, two turns — that's what makes it a conversation.
    assert len(aam.list_conversations(row["id"])) == 1


@pytest.mark.asyncio
async def test_agent_is_addressed_by_name_and_defaults_to_the_apps_own(
    aam, am, db, turns
):
    row = await _make_app(am, db)
    other = await AgentManager(db).create_agent(name="Vera")

    out = await aam.ask(row, message="hi")
    assert aam.session_mgr.get_session(out["conversation_id"]).agent_id == row["agent_id"]

    named = await aam.ask(row, message="hi", agent="vera")
    assert aam.session_mgr.get_session(named["conversation_id"]).agent_id == other["id"]

    with pytest.raises(AppAgentError) as e:
        await aam.ask(row, message="hi", agent="nobody")
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_listing_agents_says_who_can_be_addressed(aam, am, db):
    await _make_app(am, db)
    await AgentManager(db).create_agent(name="Vera", description="researcher")
    listed = await aam.list_agents()
    assert "Vera" in [a["name"] for a in listed]
    # Nothing about how an agent is wired up leaks to an app.
    assert set(listed[0]) == {"name", "description", "avatar", "is_default"}


@pytest.mark.asyncio
async def test_a_conversation_belongs_to_exactly_one_app(aam, am, db, turns):
    a = await _make_app(am, db, name="SmartReader")
    b = await _make_app(am, db, name="Other App")
    out = await aam.ask(a, message="hi")

    with pytest.raises(AppAgentError) as e:
        await aam.ask(b, message="hi", conversation_id=out["conversation_id"])
    # 404, not 403: a different answer would confirm the thread exists.
    assert e.value.status_code == 404
    assert aam.list_conversations(b["id"]) == []


@pytest.mark.asyncio
async def test_transcript_comes_back_for_a_reload(aam, am, db, turns, mgr):
    row = await _make_app(am, db)
    out = await aam.ask(row, message="What does it say?")
    # The fake harness never persists, so write what a real turn would.
    session = mgr.get_session(out["conversation_id"])
    await mgr._persist_message(
        session,
        MessageContent(role="assistant", type="text", content=turns.reply),
    )
    detail = await aam.get_conversation(row["id"], out["conversation_id"])
    assert detail["id"] == out["conversation_id"]
    assert [m["text"] for m in detail["messages"]] == [turns.reply]


@pytest.mark.asyncio
async def test_deleting_a_conversation_and_deleting_the_app(aam, am, db, turns):
    row = await _make_app(am, db)
    first = await aam.ask(row, message="one")
    second = await aam.ask(row, message="two")
    assert len(aam.list_conversations(row["id"])) == 2

    await aam.delete_conversation(row["id"], first["conversation_id"])
    assert [c["id"] for c in aam.list_conversations(row["id"])] == [
        second["conversation_id"]
    ]

    # Deleting the application takes the rest with it — they belong to a
    # program that no longer exists.
    app_agent_mod.app_agent_manager.bind(
        session_mgr=aam.session_mgr, db=db, app_mgr=am
    )
    try:
        await am.delete_application(row["id"])
    finally:
        app_agent_mod.app_agent_manager.shutdown()
    assert aam.session_mgr.get_session(second["conversation_id"]) is None


# ------------------------------------------------------------------ caps


@pytest.mark.asyncio
async def test_message_and_context_are_bounded(aam, am, db, turns):
    row = await _make_app(am, db)
    with pytest.raises(AppAgentError) as e:
        await aam.ask(row, message="x" * (app_agent_mod.MAX_MESSAGE_CHARS + 1))
    assert e.value.status_code == 413
    with pytest.raises(AppAgentError) as e:
        await aam.ask(
            row, message="hi", context="x" * (app_agent_mod.MAX_CONTEXT_CHARS + 1)
        )
    assert e.value.status_code == 413
    with pytest.raises(AppAgentError) as e:
        await aam.ask(row, message="   ")
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_conversations_per_app_are_bounded(aam, am, db, turns, monkeypatch):
    row = await _make_app(am, db)
    monkeypatch.setattr(app_agent_mod, "MAX_CONVERSATIONS_PER_APP", 2)
    await aam.ask(row, message="one")
    await aam.ask(row, message="two")
    with pytest.raises(AppAgentError) as e:
        await aam.ask(row, message="three")
    assert e.value.status_code == 429
    assert "reuse a conversation_id" in e.value.message


@pytest.mark.asyncio
async def test_turns_in_flight_are_bounded(aam, am, db, turns, monkeypatch):
    row = await _make_app(am, db)
    turns.emit = False  # nothing finishes, so the turns stay in flight
    monkeypatch.setattr(app_agent_mod, "MAX_ACTIVE_TURNS_PER_APP", 1)
    await aam.begin_turn(row, message="one")
    with pytest.raises(AppAgentError) as e:
        await aam.begin_turn(row, message="two")
    assert e.value.status_code == 429


@pytest.mark.asyncio
async def test_a_conversation_answers_one_thing_at_a_time(aam, am, db, turns):
    row = await _make_app(am, db)
    turns.emit = False
    started = await aam.begin_turn(row, message="one")
    with pytest.raises(AppAgentError) as e:
        await aam.begin_turn(
            row, message="two", conversation_id=started.session_id
        )
    assert e.value.status_code == 409


@pytest.mark.asyncio
async def test_a_turn_still_unwinding_is_waited_out_not_refused(
    aam, am, db, mgr, turns, monkeypatch
):
    """A real turn broadcasts its result and only *then* finishes unwinding, so
    an app that sends the next message the moment a reply lands would otherwise
    be told its own conversation is busy."""
    row = await _make_app(am, db)
    unwinding = asyncio.Event()

    async def start(session_id, prompt, attachment_ids=None, **kw):
        session = mgr.get_session(session_id)

        async def tail():
            await unwinding.wait()

        session._active_task = asyncio.ensure_future(tail())
        await mgr._broadcast(
            {"type": "result", "session_id": session_id, "is_error": False}
        )

    monkeypatch.setattr(mgr, "start_message", start)
    first = await aam.ask(row, message="one")

    # The previous turn's task is still alive; release it a beat later.
    async def release():
        await asyncio.sleep(0.05)
        unwinding.set()

    asyncio.ensure_future(release())
    second = await aam.ask(
        row, message="two", conversation_id=first["conversation_id"]
    )
    assert second["conversation_id"] == first["conversation_id"]


@pytest.mark.asyncio
async def test_a_conversation_busy_with_someone_elses_turn_is_refused(
    aam, am, db, mgr, turns, monkeypatch
):
    row = await _make_app(am, db)
    monkeypatch.setattr(app_agent_mod, "IDLE_WAIT_SECONDS", 0.05)

    async def start(session_id, prompt, attachment_ids=None, **kw):
        session = mgr.get_session(session_id)
        session._active_task = asyncio.ensure_future(asyncio.sleep(30))
        await mgr._broadcast(
            {"type": "result", "session_id": session_id, "is_error": False}
        )

    monkeypatch.setattr(mgr, "start_message", start)
    first = await aam.ask(row, message="one")
    with pytest.raises(AppAgentError) as e:
        await aam.ask(row, message="two", conversation_id=first["conversation_id"])
    assert e.value.status_code == 409

    # Tidy the stand-in turn up rather than leaving a cancelled task for the
    # loop to complain about at collection time.
    task = mgr.get_session(first["conversation_id"])._active_task
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_an_errored_turn_is_reported_not_swallowed(aam, am, db, turns):
    row = await _make_app(am, db)
    turns.is_error = True
    with pytest.raises(AppAgentError) as e:
        await aam.ask(row, message="hi")
    assert e.value.status_code == 502


@pytest.mark.asyncio
async def test_ask_that_outlives_its_wait_says_where_the_answer_went(
    aam, am, db, turns, monkeypatch
):
    row = await _make_app(am, db)
    turns.emit = False
    monkeypatch.setattr(app_agent_mod, "ASK_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(AppAgentError) as e:
        await aam.ask(row, message="hi")
    assert e.value.status_code == 504
    # The record is released, so the conversation isn't wedged forever.
    assert aam._turns == {}


# ---------------------------------------------------------------- the stream


@pytest.mark.asyncio
async def test_stream_frames_arrive_in_order(aam, am, db, turns):
    row = await _make_app(am, db)
    turn = await aam.begin_turn(row, message="hi")
    events = [e async for e in aam.stream_turn(turn)]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "conversation"
    assert kinds[-1] == "done"
    assert "delta" in kinds and "message" in kinds and "tool" in kinds
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "Sure — the article "
    # A tool event names the tool and nothing else: its arguments are none of
    # the app's business.
    assert [e for e in events if e["type"] == "tool"] == [
        {"type": "tool", "name": "WebFetch"}
    ]
    assert events[-1]["reply"] == turns.reply


@pytest.mark.asyncio
async def test_a_question_reaches_the_app(aam, am, db, mgr, turns, monkeypatch):
    row = await _make_app(am, db)

    async def ask_then_question(session_id, prompt, attachment_ids=None, **kw):
        await mgr._broadcast(
            {
                "type": "question_request",
                "session_id": session_id,
                "question_id": "q1",
                "questions": [{"question": "Which one?"}],
            }
        )
        await mgr._broadcast(
            {"type": "result", "session_id": session_id, "is_error": False}
        )

    monkeypatch.setattr(mgr, "start_message", ask_then_question)
    turn = await aam.begin_turn(row, message="hi")
    events = [e async for e in aam.stream_turn(turn)]
    question = next(e for e in events if e["type"] == "question")
    assert question["question_id"] == "q1"
    assert question["questions"][0]["question"] == "Which one?"


# ------------------------------------------------------------------- HTTP


@pytest.fixture
async def client(apps_root, monkeypatch):
    """The real ASGI app, wired to a fresh DB and the singleton managers."""
    database = Database(":memory:")
    await database.initialize()
    session_manager.sessions.clear()
    await session_manager.initialize(database)

    agents_mod.set_manager(AgentManager(database))
    singleton_application_manager.bind(session_mgr=session_manager, db=database)
    applications_mod.set_manager(singleton_application_manager)
    app_agent_mod.app_agent_manager.bind(
        session_mgr=session_manager,
        db=database,
        app_mgr=singleton_application_manager,
    )

    fake = FakeTurns(session_manager)
    monkeypatch.setattr(session_manager, "start_message", fake.start_message)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.turns = fake  # type: ignore[attr-defined]
        yield c

    app_agent_mod.app_agent_manager.shutdown()
    singleton_application_manager.shutdown()
    await database.close()


async def _api_app(client, name="SmartReader") -> dict:
    agents = (await client.get("/api/agents", headers=HEADERS)).json()
    resp = await client.post(
        "/api/applications",
        json={
            "name": name,
            "description": "Reads a page and talks about it",
            "agent_id": agents[0]["id"],
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_http_requires_a_credential(client):
    row = await _api_app(client)
    assert (await client.get(f"/apps/{row['id']}/agent/agents")).status_code == 401
    assert (
        await client.post(
            f"/apps/{row['id']}/agent/ask", json={"message": "hi"}
        )
    ).status_code == 401


@pytest.mark.asyncio
async def test_http_accepts_the_apps_own_token_and_only_its_own(client):
    a = await _api_app(client, "SmartReader")
    b = await _api_app(client, "Other App")
    scoped = {"X-Octopus-App-Token": app_scope_token(a["id"])}

    assert (
        await client.get(f"/apps/{a['id']}/agent/agents", headers=scoped)
    ).status_code == 200
    # The same token against another application is simply not a credential.
    assert (
        await client.get(f"/apps/{b['id']}/agent/agents", headers=scoped)
    ).status_code == 401
    # It also works as a bearer, which is what most HTTP clients make easy.
    assert (
        await client.get(
            f"/apps/{a['id']}/agent/agents",
            headers={"Authorization": f"Bearer {app_scope_token(a['id'])}"},
        )
    ).status_code == 200


@pytest.mark.asyncio
async def test_http_ask_and_conversation_roundtrip(client):
    row = await _api_app(client)
    cookies = APP_COOKIE

    resp = await client.post(
        f"/apps/{row['id']}/agent/ask",
        json={"message": "What does it say?", "context": "ARTICLE"},
        headers=cookies,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reply"] == client.turns.reply
    cid = body["conversation_id"]

    listed = (
        await client.get(f"/apps/{row['id']}/agent/conversations", headers=cookies)
    ).json()
    assert [c["id"] for c in listed] == [cid]

    one = await client.get(
        f"/apps/{row['id']}/agent/conversations/{cid}", headers=cookies
    )
    assert one.status_code == 200
    assert one.json()["id"] == cid

    assert (
        await client.delete(
            f"/apps/{row['id']}/agent/conversations/{cid}", headers=cookies
        )
    ).status_code == 204
    assert (
        await client.get(
            f"/apps/{row['id']}/agent/conversations/{cid}", headers=cookies
        )
    ).status_code == 404


@pytest.mark.asyncio
async def test_http_chat_streams_server_sent_events(client):
    row = await _api_app(client)
    async with client.stream(
        "POST",
        f"/apps/{row['id']}/agent/chat",
        json={"message": "hi"},
        headers=APP_COOKIE,
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-store"
        text = "".join([chunk async for chunk in resp.aiter_text()])

    names = [ln[len("event: "):] for ln in text.splitlines() if ln.startswith("event: ")]
    assert names[0] == "conversation" and names[-1] == "done"
    payloads = [
        json.loads(ln[len("data: "):]) for ln in text.splitlines()
        if ln.startswith("data: ")
    ]
    # The type is in both places on purpose — either client style works.
    assert payloads[0]["type"] == "conversation"
    assert payloads[-1]["reply"] == client.turns.reply


@pytest.mark.asyncio
async def test_http_refusals_carry_their_status(client):
    row = await _api_app(client)
    cookies = APP_COOKIE
    assert (
        await client.post(
            f"/apps/{row['id']}/agent/ask",
            json={"message": "hi", "agent": "nobody"},
            headers=cookies,
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/apps/{row['id']}/agent/ask",
            json={"message": "x" * (app_agent_mod.MAX_MESSAGE_CHARS + 1)},
            headers=cookies,
        )
    ).status_code == 413
    assert (
        await client.post(
            f"/apps/{row['id']}/agent/ask",
            json={"message": "hi", "conversation_id": "nope"},
            headers=cookies,
        )
    ).status_code == 404


@pytest.mark.asyncio
async def test_app_conversations_are_marked_on_the_session_api(client):
    row = await _api_app(client)
    body = (
        await client.post(
            f"/apps/{row['id']}/agent/ask",
            json={"message": "hi"},
            headers=APP_COOKIE,
        )
    ).json()

    sessions = (await client.get("/api/sessions", headers=HEADERS)).json()
    conv = next(s for s in sessions if s["id"] == body["conversation_id"])
    # Both fields are what the sidebar filters on.
    assert conv["origin"] == ORIGIN_APP
    assert conv["app_id"] == row["id"]


@pytest.mark.asyncio
async def test_app_id_survives_a_restart(db, apps_root, mgr, am, aam, turns):
    row = await _make_app(am, db)
    out = await aam.ask(row, message="hi")

    reborn = SessionManager()
    await reborn.initialize(db)
    session = reborn.get_session(out["conversation_id"])
    assert session is not None
    assert session.app_id == row["id"]
    assert session.origin == ORIGIN_APP
