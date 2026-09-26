"""Tests for agent-built applications (docs/plans/applications.md).

Covers:
  * Path helpers — slug allocation + de-duplication, entrypoint validation,
    and the traversal guard the static route depends on.
  * `ApplicationManager` — create wires a build session (origin='application',
    working_dir = the app dir) and fires the brief; the broadcast subscriber
    turns build-session turns into building/ready/failed; follow-up builds
    reuse the session (and rebuild one when it's gone); rename/entrypoint
    updates; delete removes the directory but only inside the managed root.
  * The REST routes under `/api/applications`.
  * The static route `/apps/{id}/…` — bearer / query / cookie auth, directory
    → entrypoint, traversal → 404, no-store.

No real harness ever runs: `start_message` is patched so the captured prompt
is what's asserted, exactly like the delegation suite.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server.agent_manager import AgentManager
from server.applications import (
    ApplicationError,
    ApplicationManager,
    allocate_app_dir,
    is_inside_root,
    is_safe_relative_path,
    resolve_within,
    slugify,
)
from server.applications import (
    application_manager as singleton_application_manager,
)
from server.config import settings
from server.database import Database
from server.main import app
from server.routers import agents as agents_mod
from server.routers import applications as applications_mod
from server.session_manager import SessionManager, session_manager

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    """Per-test ApplicationManager bound to the per-test session manager."""
    m = ApplicationManager()
    m.bind(session_mgr=mgr, db=db)
    yield m
    m.shutdown()


@pytest.fixture
def sent(mgr, monkeypatch):
    """Capture `start_message` calls instead of spawning a harness."""
    calls: list[tuple[str, str]] = []

    async def fake_start_message(session_id, prompt, attachment_ids=None):
        calls.append((session_id, prompt))

    monkeypatch.setattr(mgr, "start_message", fake_start_message)
    return calls


async def _make_agent(db, name: str = "Octo Builder") -> dict:
    return await AgentManager(db).create_agent(name=name)


async def _create_app(am, db, *, name="Todo App", **extra) -> dict:
    agent = extra.pop("agent", None) or await _make_agent(db, extra.pop("agent_name", name + " Agent"))
    return await am.create_application(
        name=name,
        description=extra.pop("description", "A todo list with checkboxes"),
        agent_id=agent["id"],
        **extra,
    )


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def test_slugify_is_filesystem_safe():
    assert slugify("My Todo App!") == "my-todo-app"
    assert slugify("  ---  ") == "app"
    assert slugify("") == "app"
    assert len(slugify("x" * 200)) <= 48


def test_allocate_app_dir_dedupes(apps_root):
    first = allocate_app_dir("Notes")
    os.makedirs(first)
    second = allocate_app_dir("Notes")
    assert os.path.basename(first) == "notes"
    assert os.path.basename(second) == "notes-2"
    os.makedirs(second)
    assert os.path.basename(allocate_app_dir("notes")) == "notes-3"


def test_is_safe_relative_path():
    assert is_safe_relative_path("index.html")
    assert is_safe_relative_path("assets/app.js")
    assert not is_safe_relative_path("")
    assert not is_safe_relative_path("/etc/passwd")
    assert not is_safe_relative_path("../outside.html")
    assert not is_safe_relative_path("assets/../../outside.html")


def test_resolve_within_blocks_traversal(tmp_path):
    base = tmp_path / "app"
    base.mkdir()
    (base / "index.html").write_text("hi")
    (tmp_path / "secret.txt").write_text("nope")

    assert resolve_within(str(base), "index.html") == str(base / "index.html")
    assert resolve_within(str(base), "") == str(base)
    assert resolve_within(str(base), "../secret.txt") is None
    assert resolve_within(str(base), "/../secret.txt") is None
    assert resolve_within(str(base), "a/b/../../../secret.txt") is None


def test_resolve_within_blocks_escaping_symlink(tmp_path):
    base = tmp_path / "app"
    base.mkdir()
    (tmp_path / "secret.txt").write_text("nope")
    os.symlink(tmp_path / "secret.txt", base / "link.txt")
    assert resolve_within(str(base), "link.txt") is None


def test_is_inside_root_guards_the_managed_tree(apps_root, tmp_path):
    os.makedirs(apps_root, exist_ok=True)
    assert is_inside_root(str(apps_root / "todo"))
    # The root itself is not "inside" it — deleting it would take every app.
    assert not is_inside_root(str(apps_root))
    assert not is_inside_root(str(tmp_path / "elsewhere"))
    assert not is_inside_root("/")


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_makes_dir_row_and_build_session(am, db, mgr, sent):
    agent = await _make_agent(db)
    row = await am.create_application(
        name="Todo App",
        description="A todo list with checkboxes",
        agent_id=agent["id"],
        icon="✅",
    )

    assert row["status"] == "building"
    assert row["icon"] == "✅"
    assert row["entrypoint"] == "index.html"
    assert os.path.isdir(row["app_dir"])
    assert os.path.basename(row["app_dir"]) == "todo-app"

    session = mgr.get_session(row["session_id"])
    assert session is not None
    assert session.origin == "application"
    assert session.working_dir == row["app_dir"]
    assert session.agent_id == agent["id"]
    assert session.name == "Build: Todo App"

    # The brief went out on that session and carries the rendering contract.
    assert len(sent) == 1
    sid, prompt = sent[0]
    assert sid == session.id
    assert "Todo App" in prompt
    assert "A todo list with checkboxes" in prompt
    assert row["app_dir"] in prompt
    assert "index.html" in prompt
    assert "STATIC FILES" in prompt


@pytest.mark.asyncio
async def test_create_appends_extra_instructions(am, db, sent):
    agent = await _make_agent(db)
    await am.create_application(
        name="Notes",
        description="A notepad",
        agent_id=agent["id"],
        instructions="Use a dark theme and a monospace font.",
    )
    assert "Use a dark theme and a monospace font." in sent[0][1]


@pytest.mark.asyncio
async def test_create_rejects_duplicate_name_case_insensitively(am, db, sent):
    agent = await _make_agent(db)
    await am.create_application(
        name="Todo", description="d", agent_id=agent["id"]
    )
    with pytest.raises(ApplicationError) as e:
        await am.create_application(
            name="  todo  ", description="d", agent_id=agent["id"]
        )
    assert e.value.status_code == 409


@pytest.mark.asyncio
async def test_create_requires_name_description_and_agent(am, db, sent):
    agent = await _make_agent(db)
    with pytest.raises(ApplicationError):
        await am.create_application(name="  ", description="d", agent_id=agent["id"])
    with pytest.raises(ApplicationError):
        await am.create_application(name="X", description=" ", agent_id=agent["id"])
    with pytest.raises(ApplicationError) as e:
        await am.create_application(name="X", description="d", agent_id="nope")
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_create_rejects_escaping_entrypoint(am, db, sent):
    agent = await _make_agent(db)
    with pytest.raises(ApplicationError):
        await am.create_application(
            name="X", description="d", agent_id=agent["id"], entrypoint="../x.html"
        )


@pytest.mark.asyncio
async def test_create_marks_failed_when_the_build_session_wont_start(
    am, db, mgr, monkeypatch
):
    async def boom(session_id, prompt, attachment_ids=None):
        raise RuntimeError("harness exploded")

    monkeypatch.setattr(mgr, "start_message", boom)
    agent = await _make_agent(db)
    row = await am.create_application(
        name="Doomed", description="d", agent_id=agent["id"]
    )
    assert row["status"] == "failed"
    assert "harness exploded" in row["error"]


# ---------------------------------------------------------------------------
# Status derivation from the build session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_with_entrypoint_flips_to_ready(am, db, mgr, sent):
    row = await _create_app(am, db)
    open(os.path.join(row["app_dir"], "index.html"), "w").write("<h1>hi</h1>")

    await mgr._broadcast({"type": "result", "session_id": row["session_id"]})

    updated = await db.get_application(row["id"])
    assert updated["status"] == "ready"
    assert updated["error"] is None
    assert updated["last_built_at"]


@pytest.mark.asyncio
async def test_result_without_entrypoint_flips_to_failed(am, db, mgr, sent):
    row = await _create_app(am, db)
    await mgr._broadcast({"type": "result", "session_id": row["session_id"]})
    updated = await db.get_application(row["id"])
    assert updated["status"] == "failed"
    assert "index.html" in updated["error"]


@pytest.mark.asyncio
async def test_error_event_flips_to_failed_with_the_message(am, db, mgr, sent):
    row = await _create_app(am, db)
    await mgr._broadcast(
        {"type": "error", "session_id": row["session_id"], "message": "credit exhausted"}
    )
    updated = await db.get_application(row["id"])
    assert updated["status"] == "failed"
    assert updated["error"] == "credit exhausted"


@pytest.mark.asyncio
async def test_a_written_app_stays_ready_even_if_the_turn_errored(am, db, mgr, sent):
    """The filesystem is the source of truth: an agent that wrote a perfectly
    good app and *then* hit an error still produced a working application."""
    row = await _create_app(am, db)
    open(os.path.join(row["app_dir"], "index.html"), "w").write("<h1>hi</h1>")
    await mgr._broadcast(
        {"type": "error", "session_id": row["session_id"], "message": "late failure"}
    )
    assert (await db.get_application(row["id"]))["status"] == "ready"


@pytest.mark.asyncio
async def test_running_status_returns_the_app_to_building(am, db, mgr, sent):
    row = await _create_app(am, db)
    open(os.path.join(row["app_dir"], "index.html"), "w").write("<h1>hi</h1>")
    await mgr._broadcast({"type": "result", "session_id": row["session_id"]})
    assert (await db.get_application(row["id"]))["status"] == "ready"

    # A change typed straight into the build session's chat — no REST call.
    await mgr._broadcast(
        {"type": "status", "session_id": row["session_id"], "status": "running"}
    )
    assert (await db.get_application(row["id"]))["status"] == "building"

    await mgr._broadcast(
        {"type": "status", "session_id": row["session_id"], "status": "idle"}
    )
    assert (await db.get_application(row["id"]))["status"] == "building"


@pytest.mark.asyncio
async def test_events_for_other_sessions_are_ignored(am, db, mgr, sent):
    row = await _create_app(am, db)
    agent = await _make_agent(db, "Bystander")
    other = await mgr.create_session(agent_id=agent["id"], name="unrelated")
    open(os.path.join(row["app_dir"], "index.html"), "w").write("x")
    await mgr._broadcast({"type": "result", "session_id": other.id})
    assert (await db.get_application(row["id"]))["status"] == "building"


@pytest.mark.asyncio
async def test_broadcasts_carry_the_row(am, db, mgr, sent):
    seen: list[dict] = []

    mgr.on_broadcast("test-sink", lambda m: seen.append(m))
    row = await _create_app(am, db)
    created = [m for m in seen if m["type"] == "application_created"]
    assert created and created[0]["application"]["id"] == row["id"]

    open(os.path.join(row["app_dir"], "index.html"), "w").write("x")
    await mgr._broadcast({"type": "result", "session_id": row["session_id"]})
    updated = [m for m in seen if m["type"] == "application_updated"]
    assert updated and updated[-1]["application"]["status"] == "ready"
    mgr.remove_broadcast("test-sink")


# ---------------------------------------------------------------------------
# Follow-up builds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_build_reuses_the_session(am, db, mgr, sent):
    row = await _create_app(am, db)
    sent.clear()

    updated = await am.request_build(row["id"], "Add a dark mode toggle")
    assert updated["status"] == "building"
    assert len(sent) == 1
    sid, prompt = sent[0]
    assert sid == row["session_id"]
    assert "Add a dark mode toggle" in prompt
    assert row["app_dir"] in prompt
    # A follow-up doesn't re-send the whole brief — the session has it.
    assert "STATIC FILES" not in prompt


@pytest.mark.asyncio
async def test_request_build_clears_a_previous_failure(am, db, mgr, sent):
    row = await _create_app(am, db)
    await mgr._broadcast({"type": "result", "session_id": row["session_id"]})
    assert (await db.get_application(row["id"]))["status"] == "failed"

    await am.request_build(row["id"], "try again")
    after = await db.get_application(row["id"])
    assert after["status"] == "building"
    assert after["error"] is None


@pytest.mark.asyncio
async def test_request_build_opens_a_new_session_when_the_old_one_is_gone(
    am, db, mgr, sent
):
    row = await _create_app(am, db)
    await mgr.delete_session(row["session_id"])
    sent.clear()

    updated = await am.request_build(row["id"], "Add a footer")
    assert updated["session_id"] != row["session_id"]
    assert mgr.get_session(updated["session_id"]) is not None
    # No transcript to inherit → the agent gets the full brief again.
    assert "STATIC FILES" in sent[0][1]
    assert "Add a footer" in sent[0][1]


@pytest.mark.asyncio
async def test_request_build_rejects_an_empty_prompt(am, db, sent):
    row = await _create_app(am, db)
    with pytest.raises(ApplicationError):
        await am.request_build(row["id"], "   ")


@pytest.mark.asyncio
async def test_request_build_on_a_missing_application_is_404(am, db, sent):
    with pytest.raises(ApplicationError) as e:
        await am.request_build("nope", "hi")
    assert e.value.status_code == 404


# ---------------------------------------------------------------------------
# Update / delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_keeps_the_directory(am, db, sent):
    row = await _create_app(am, db, name="Todo App")
    renamed = await am.update_application(row["id"], name="Task Board")
    assert renamed["name"] == "Task Board"
    assert renamed["app_dir"] == row["app_dir"]


@pytest.mark.asyncio
async def test_rename_to_an_existing_name_is_409(am, db, sent):
    agent = await _make_agent(db)
    a = await am.create_application(name="A", description="d", agent_id=agent["id"])
    await am.create_application(name="B", description="d", agent_id=agent["id"])
    with pytest.raises(ApplicationError) as e:
        await am.update_application(a["id"], name="b")
    assert e.value.status_code == 409


@pytest.mark.asyncio
async def test_entrypoint_change_re_derives_status(am, db, mgr, sent):
    row = await _create_app(am, db)
    open(os.path.join(row["app_dir"], "main.html"), "w").write("<h1>hi</h1>")
    await mgr._broadcast({"type": "result", "session_id": row["session_id"]})
    assert (await db.get_application(row["id"]))["status"] == "failed"

    updated = await am.update_application(row["id"], entrypoint="main.html")
    assert updated["entrypoint"] == "main.html"
    assert updated["status"] == "ready"


@pytest.mark.asyncio
async def test_archive_hides_it_but_keeps_row_and_files(am, db, sent):
    row = await _create_app(am, db)
    open(os.path.join(row["app_dir"], "index.html"), "w").write("<h1>hi</h1>")

    archived = await am.set_archived(row["id"], True)
    assert archived["archived"] is True
    assert [a["id"] for a in await am.list_applications()] == []
    assert [a["id"] for a in await am.list_applications(only_archived=True)] == [
        row["id"]
    ]
    # The whole point of archiving over deleting: the app is still on disk.
    assert os.path.isfile(os.path.join(row["app_dir"], "index.html"))

    restored = await am.set_archived(row["id"], False)
    assert restored["archived"] is False
    assert [a["id"] for a in await am.list_applications()] == [row["id"]]


@pytest.mark.asyncio
async def test_archiving_twice_is_a_no_op(am, db, sent):
    row = await _create_app(am, db)
    await am.set_archived(row["id"], True)
    again = await am.set_archived(row["id"], True)
    assert again["archived"] is True


@pytest.mark.asyncio
async def test_restore_refuses_a_name_taken_since(am, db, sent):
    agent = await _make_agent(db)
    row = await am.create_application(
        name="Tracker", description="d", agent_id=agent["id"]
    )
    await am.set_archived(row["id"], True)
    # The unique index only covers live rows, so the name is free again.
    await am.create_application(
        name="Tracker", description="d2", agent_id=agent["id"]
    )
    with pytest.raises(ApplicationError) as e:
        await am.set_archived(row["id"], False)
    assert e.value.status_code == 409


@pytest.mark.asyncio
async def test_delete_removes_row_and_directory(am, db, sent):
    row = await _create_app(am, db)
    await am.delete_application(row["id"])
    assert await db.get_application(row["id"]) is None
    assert not os.path.exists(row["app_dir"])


@pytest.mark.asyncio
async def test_delete_can_keep_the_files(am, db, sent):
    row = await _create_app(am, db)
    await am.delete_application(row["id"], keep_files=True)
    assert await db.get_application(row["id"]) is None
    assert os.path.isdir(row["app_dir"])


@pytest.mark.asyncio
async def test_delete_refuses_to_touch_a_dir_outside_the_root(
    am, db, tmp_path, sent
):
    row = await _create_app(am, db)
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keepme.txt").write_text("hi")
    await db.update_application(row["id"], app_dir=str(outside))

    await am.delete_application(row["id"])
    assert await db.get_application(row["id"]) is None
    assert (outside / "keepme.txt").exists()


@pytest.mark.asyncio
async def test_delete_keeps_the_build_session(am, db, mgr, sent):
    row = await _create_app(am, db)
    await am.delete_application(row["id"])
    assert mgr.get_session(row["session_id"]) is not None


# ---------------------------------------------------------------------------
# REST routes
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(apps_root, monkeypatch):
    """The real app wired to a fresh DB + the singleton managers, with
    `start_message` stubbed so no harness ever spawns."""
    db = Database(":memory:")
    await db.initialize()
    session_manager.sessions.clear()
    await session_manager.initialize(db)

    agents_mod.set_manager(AgentManager(db))
    singleton_application_manager.bind(session_mgr=session_manager, db=db)
    applications_mod.set_manager(singleton_application_manager)

    calls: list[tuple[str, str]] = []

    async def fake_start_message(session_id, prompt, attachment_ids=None):
        calls.append((session_id, prompt))

    monkeypatch.setattr(session_manager, "start_message", fake_start_message)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.sent = calls  # type: ignore[attr-defined]
        yield c

    singleton_application_manager.shutdown()
    await db.close()


async def _api_create(client, name="Todo App", **extra) -> dict:
    agents = (await client.get("/api/agents", headers=HEADERS)).json()
    body = {
        "name": name,
        "description": "A todo list",
        "agent_id": extra.pop("agent_id", agents[0]["id"]),
        **extra,
    }
    resp = await client.post("/api/applications", json=body, headers=HEADERS)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_api_auth_required(client):
    assert (await client.get("/api/applications")).status_code in (401, 403)


@pytest.mark.asyncio
async def test_api_create_list_get(client):
    created = await _api_create(client)
    assert created["status"] == "building"
    assert created["session_id"]

    listed = (await client.get("/api/applications", headers=HEADERS)).json()
    assert [a["id"] for a in listed] == [created["id"]]

    one = await client.get(f"/api/applications/{created['id']}", headers=HEADERS)
    assert one.status_code == 200
    assert one.json()["name"] == "Todo App"

    assert (
        await client.get("/api/applications/missing", headers=HEADERS)
    ).status_code == 404


@pytest.mark.asyncio
async def test_api_create_validation(client):
    agents = (await client.get("/api/agents", headers=HEADERS)).json()
    # Pydantic rejects the empty name/description before the manager sees them.
    resp = await client.post(
        "/api/applications",
        json={"name": "", "description": "d", "agent_id": agents[0]["id"]},
        headers=HEADERS,
    )
    assert resp.status_code == 422
    resp = await client.post(
        "/api/applications",
        json={"name": "X", "description": "d", "agent_id": "ghost"},
        headers=HEADERS,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_duplicate_name_is_409(client):
    await _api_create(client, name="Dup")
    agents = (await client.get("/api/agents", headers=HEADERS)).json()
    resp = await client.post(
        "/api/applications",
        json={"name": "dup", "description": "d", "agent_id": agents[0]["id"]},
        headers=HEADERS,
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_api_patch_and_build_and_delete(client):
    created = await _api_create(client)

    patched = await client.patch(
        f"/api/applications/{created['id']}",
        json={"name": "Renamed", "icon": "🧮"},
        headers=HEADERS,
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed"
    assert patched.json()["icon"] == "🧮"

    client.sent.clear()  # type: ignore[attr-defined]
    built = await client.post(
        f"/api/applications/{created['id']}/build",
        json={"prompt": "make it blue"},
        headers=HEADERS,
    )
    assert built.status_code == 200
    assert built.json()["status"] == "building"
    assert "make it blue" in client.sent[0][1]  # type: ignore[attr-defined]

    resp = await client.delete(
        f"/api/applications/{created['id']}", headers=HEADERS
    )
    assert resp.status_code == 204
    assert (await client.get("/api/applications", headers=HEADERS)).json() == []


@pytest.mark.asyncio
async def test_api_archive_and_restore(client):
    created = await _api_create(client, name="Archivable")

    resp = await client.post(
        f"/api/applications/{created['id']}/archive", headers=HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["archived"] is True
    assert (await client.get("/api/applications", headers=HEADERS)).json() == []
    archived = await client.get(
        "/api/applications", params={"archived": "true"}, headers=HEADERS
    )
    assert [a["id"] for a in archived.json()] == [created["id"]]

    resp = await client.post(
        f"/api/applications/{created['id']}/unarchive", headers=HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["archived"] is False
    live = (await client.get("/api/applications", headers=HEADERS)).json()
    assert [a["id"] for a in live] == [created["id"]]


@pytest.mark.asyncio
async def test_api_build_requires_a_prompt(client):
    created = await _api_create(client)
    resp = await client.post(
        f"/api/applications/{created['id']}/build", json={"prompt": ""}, headers=HEADERS
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Static serving
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_serves_the_entrypoint_for_the_app_root(client):
    created = await _api_create(client, name="Served")
    with open(os.path.join(created["app_dir"], "index.html"), "w") as f:
        f.write("<h1>Hello Octopus</h1>")

    resp = await client.get(f"/apps/{created['id']}/", headers=HEADERS)
    assert resp.status_code == 200
    assert "Hello Octopus" in resp.text
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_static_serves_nested_assets(client):
    created = await _api_create(client, name="Assets")
    os.makedirs(os.path.join(created["app_dir"], "assets"))
    with open(os.path.join(created["app_dir"], "assets", "app.js"), "w") as f:
        f.write("console.log(1)")

    resp = await client.get(f"/apps/{created['id']}/assets/app.js", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.text == "console.log(1)"
    assert "javascript" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_static_accepts_query_token_and_cookie(client):
    created = await _api_create(client, name="Authy")
    with open(os.path.join(created["app_dir"], "index.html"), "w") as f:
        f.write("ok")

    # No credentials at all.
    assert (await client.get(f"/apps/{created['id']}/")).status_code == 401
    # ?token= — what an "open in a new tab" link uses.
    assert (
        await client.get(f"/apps/{created['id']}/", params={"token": TOKEN})
    ).status_code == 200
    # The cookie the SPA sets before mounting the iframe. Set on the client
    # rather than per request: httpx deprecated per-request `cookies=` because
    # what it should do about persistence is ambiguous, and a browser sends the
    # cookie jar it has — which is what setting it on the client models.
    client.cookies.set("octopus_app_token", TOKEN)
    assert (await client.get(f"/apps/{created['id']}/")).status_code == 200
    client.cookies.set("octopus_app_token", "wrong")
    assert (await client.get(f"/apps/{created['id']}/")).status_code == 401
    client.cookies.clear()


@pytest.mark.asyncio
async def test_static_traversal_is_a_404(client, tmp_path):
    """Encoded traversal is what actually reaches the server — HTTP clients
    (httpx here, every browser in the wild) collapse literal `..` segments
    before sending. Both shapes are covered: the encoded ones must 404, and
    none of them may ever return the file outside the app."""
    created = await _api_create(client, name="Locked")
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")

    for path in ("..%2Fsecret.txt", "%2e%2e/secret.txt", "....//secret.txt"):
        resp = await client.get(f"/apps/{created['id']}/{path}", headers=HEADERS)
        assert resp.status_code == 404, path

    for path in ("../secret.txt", "a/../../secret.txt"):
        resp = await client.get(f"/apps/{created['id']}/{path}", headers=HEADERS)
        assert "top secret" not in resp.text, path


@pytest.mark.asyncio
async def test_static_refuses_a_symlink_out_of_the_app(client, tmp_path):
    created = await _api_create(client, name="Linked")
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    os.symlink(str(secret), os.path.join(created["app_dir"], "link.txt"))

    resp = await client.get(f"/apps/{created['id']}/link.txt", headers=HEADERS)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_static_missing_file_and_missing_app_are_404(client):
    created = await _api_create(client, name="Empty")
    assert (
        await client.get(f"/apps/{created['id']}/", headers=HEADERS)
    ).status_code == 404
    assert (await client.get("/apps/ghost/x.html", headers=HEADERS)).status_code == 404


@pytest.mark.asyncio
async def test_static_bare_app_url_redirects_to_a_trailing_slash(client):
    created = await _api_create(client, name="Redirected")
    resp = await client.get(f"/apps/{created['id']}", headers=HEADERS)
    assert resp.status_code in (307, 308)
    assert resp.headers["location"] == f"/apps/{created['id']}/"


# --------------------------------------------------------------------------- #
# Icon discovery — an application supplying its own icon
# --------------------------------------------------------------------------- #


def _app(tmp_path, name="app"):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def test_icon_found_by_convention_at_the_root(tmp_path):
    """`icon.svg` at the root is what compose_build_prompt asks agents for."""
    from server.applications import discover_icon_src

    d = _app(tmp_path)
    (Path(d) / "index.html").write_text("<html></html>")
    (Path(d) / "icon.svg").write_text("<svg/>")
    assert discover_icon_src(d, "index.html") == "icon.svg"


def test_convention_candidates_are_tried_in_priority_order(tmp_path):
    from server.applications import discover_icon_src

    d = _app(tmp_path)
    (Path(d) / "index.html").write_text("<html></html>")
    (Path(d) / "favicon.png").write_text("x")
    (Path(d) / "apple-touch-icon.png").write_text("x")
    assert discover_icon_src(d, "index.html") == "favicon.png"
    (Path(d) / "icon.svg").write_text("<svg/>")
    assert discover_icon_src(d, "index.html") == "icon.svg"


def test_icon_found_via_the_link_tag_in_a_subdirectory(tmp_path):
    """The case a convention-only implementation misses. Real apps declare
    their mark with <link rel="icon"> and keep it in a subdirectory — both apps
    on the machine this feature was requested from do exactly that."""
    from server.applications import discover_icon_src

    d = _app(tmp_path)
    (Path(d) / "assets").mkdir()
    (Path(d) / "assets" / "mark.svg").write_text("<svg/>")
    (Path(d) / "index.html").write_text(
        '<html><head><link rel="icon" href="assets/mark.svg" '
        'type="image/svg+xml" /></head></html>'
    )
    assert discover_icon_src(d, "index.html") == "assets/mark.svg"


def test_inline_data_uri_icon_is_kept_verbatim(tmp_path):
    """An inline SVG favicon IS the app's real mark. Rendered through <img
    src>, so scripts inside the SVG don't execute."""
    from server.applications import discover_icon_src

    d = _app(tmp_path)
    uri = "data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%3E%3C/svg%3E"
    (Path(d) / "index.html").write_text(
        f'<html><head><link rel="icon" href="{uri}"></head></html>'
    )
    assert discover_icon_src(d, "index.html") == uri


def test_non_image_data_uri_is_rejected(tmp_path):
    from server.applications import discover_icon_src

    d = _app(tmp_path)
    (Path(d) / "index.html").write_text(
        '<html><head><link rel="icon" href="data:text/html,<script>alert(1)</script>">'
        "</head></html>"
    )
    assert discover_icon_src(d, "index.html") is None


def test_remote_icon_is_not_adopted(tmp_path):
    """Not ours to serve, and not always reachable."""
    from server.applications import discover_icon_src

    d = _app(tmp_path)
    (Path(d) / "index.html").write_text(
        '<html><head><link rel="icon" href="https://example.com/i.png"></head></html>'
    )
    assert discover_icon_src(d, "index.html") is None


def test_icon_escaping_the_app_dir_is_rejected(tmp_path):
    """Same guard as the static route: `..` and symlinks pointing out are
    refused, so an app can't nominate a file it was never given."""
    from server.applications import discover_icon_src

    outside = tmp_path / "secret.svg"
    outside.write_text("<svg/>")
    d = _app(tmp_path, "escaper")
    (Path(d) / "index.html").write_text(
        '<html><head><link rel="icon" href="../secret.svg"></head></html>'
    )
    assert discover_icon_src(d, "index.html") is None

    # ...and via a symlink that points outside.
    d2 = _app(tmp_path, "linker")
    (Path(d2) / "index.html").write_text(
        '<html><head><link rel="icon" href="link.svg"></head></html>'
    )
    os.symlink(str(outside), str(Path(d2) / "link.svg"))
    assert discover_icon_src(d2, "index.html") is None


def test_oversized_icon_file_is_skipped(tmp_path):
    """A stray large asset isn't a sidebar icon, and would be fetched on every
    render of every row."""
    from server.applications import _MAX_ICON_BYTES, discover_icon_src

    d = _app(tmp_path)
    (Path(d) / "index.html").write_text("<html></html>")
    (Path(d) / "icon.png").write_bytes(b"x" * (_MAX_ICON_BYTES + 1))
    assert discover_icon_src(d, "index.html") is None


def test_oversized_inline_icon_is_skipped(tmp_path):
    """An inline icon ships in every API response, so it has to stay small."""
    from server.applications import _MAX_DATA_ICON_CHARS, discover_icon_src

    d = _app(tmp_path)
    huge = "data:image/svg+xml," + ("a" * (_MAX_DATA_ICON_CHARS + 1))
    (Path(d) / "index.html").write_text(
        f'<html><head><link rel="icon" href="{huge}"></head></html>'
    )
    assert discover_icon_src(d, "index.html") is None


def test_no_icon_anywhere_is_none(tmp_path):
    from server.applications import discover_icon_src

    d = _app(tmp_path)
    (Path(d) / "index.html").write_text("<html><head></head></html>")
    assert discover_icon_src(d, "index.html") is None


def test_missing_entrypoint_does_not_throw(tmp_path):
    """Discovery runs even for a failed build, where the entrypoint may never
    have been written."""
    from server.applications import discover_icon_src

    d = _app(tmp_path)
    assert discover_icon_src(d, "index.html") is None
    (Path(d) / "icon.svg").write_text("<svg/>")
    assert discover_icon_src(d, "index.html") == "icon.svg"


async def test_existing_database_migrates_to_icon_src(tmp_path):
    """An existing deployment gains the column on the next start, and its rows
    survive untouched — nullable with no default, so a row simply has no
    discovered icon until its next build.

    The "old" database is built by running the real schema and then dropping
    the column, rather than pasting a hand-written copy of last release's
    schema: a hand-written one drifts, and a drifted one tests nothing.
    """
    db_file = str(tmp_path / "old.db")
    seed = Database(db_file)
    await seed.initialize()
    await seed._conn.execute(
        "INSERT INTO applications (id, name, app_dir, entrypoint, status,"
        " created_at, updated_at) VALUES"
        " ('a1', 'Legacy', '/tmp/legacy', 'index.html', 'ready', 't', 't')"
    )
    await seed._conn.commit()
    await seed._conn.execute("ALTER TABLE applications DROP COLUMN icon_src")
    await seed._conn.commit()
    cur = await seed._conn.execute("PRAGMA table_info(applications)")
    assert "icon_src" not in [r[1] for r in await cur.fetchall()]
    await seed.close()

    # Restart against that database: the migration runs.
    db = Database(db_file)
    await db.initialize()
    try:
        cur = await db._conn.execute("PRAGMA table_info(applications)")
        assert "icon_src" in [r[1] for r in await cur.fetchall()]
        row = await db.get_application("a1")
        assert row is not None
        assert row["name"] == "Legacy"    # the old row survived
        assert row["icon_src"] is None    # and simply has no icon yet
    finally:
        await db.close()


async def test_evaluate_discovers_and_clears_the_icon(am, db, sent):
    """The icon refreshes on every evaluation: it appears when the agent writes
    one, and clears when the file is removed — without ever touching the emoji
    a user typed."""
    app = await _create_app(am, db, name="Iconic")
    app_dir = app["app_dir"]
    Path(app_dir, "index.html").write_text("<html></html>")
    Path(app_dir, "icon.svg").write_text("<svg/>")

    await am._evaluate(app["id"], broadcast=False)
    assert (await db.get_application(app["id"]))["icon_src"] == "icon.svg"

    # Agent removes it in a later build → the record must not keep pointing at
    # a file that no longer exists.
    os.remove(Path(app_dir, "icon.svg"))
    await am._evaluate(app["id"], broadcast=False)
    assert (await db.get_application(app["id"]))["icon_src"] is None


async def test_discovered_icon_never_overwrites_a_user_emoji(am, db, sent):
    """`icon` is the user's; `icon_src` is the app's. A rebuild touches only
    the second."""
    app = await _create_app(am, db, name="Emoji Keeper", icon="💠")
    Path(app["app_dir"], "index.html").write_text("<html></html>")
    Path(app["app_dir"], "icon.svg").write_text("<svg/>")

    await am._evaluate(app["id"], broadcast=False)
    row = await db.get_application(app["id"])
    assert row["icon"] == "💠"          # untouched
    assert row["icon_src"] == "icon.svg"  # and the file was still found


async def test_refresh_icons_backfills_existing_applications(am, db, sent):
    """Discovery otherwise only runs after a build, which would leave every
    application that already exists showing the generic fallback until someone
    happened to rebuild it. The acceptance criterion is "with no user action"."""
    app = await _create_app(am, db, name="Already Built")
    Path(app["app_dir"], "index.html").write_text("<html></html>")
    Path(app["app_dir"], "icon.svg").write_text("<svg/>")
    assert (await db.get_application(app["id"]))["icon_src"] is None

    assert await am.refresh_icons() == 1
    assert (await db.get_application(app["id"]))["icon_src"] == "icon.svg"

    # Idempotent: nothing changed, nothing rewritten.
    assert await am.refresh_icons() == 0


async def test_refresh_icons_leaves_the_emoji_and_status_alone(am, db, sent):
    app = await _create_app(am, db, name="Untouched", icon="💠")
    Path(app["app_dir"], "index.html").write_text("<html></html>")
    Path(app["app_dir"], "icon.svg").write_text("<svg/>")
    before = await db.get_application(app["id"])

    await am.refresh_icons()
    after = await db.get_application(app["id"])
    assert after["icon"] == "💠"
    assert after["status"] == before["status"]
    assert after["icon_src"] == "icon.svg"


# --------------------------------------------------------------------------- #
# Backends (application-backends.md)
# --------------------------------------------------------------------------- #


def _script(app_dir: str, name: str, body: str, executable: bool = True) -> None:
    import stat

    path = Path(app_dir) / name
    path.write_text(body)
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_a_backend_is_declared_by_an_executable_start_script(tmp_path):
    """Presence of `start.sh` IS the declaration — there is no manifest. A
    non-executable one reports as absent rather than being run, so a forgotten
    chmod fails as "no backend" instead of a confusing exec error."""
    from server.applications import has_backend

    d = tmp_path / "app"
    d.mkdir()
    assert has_backend(str(d)) is False

    _script(str(d), "start.sh", "#!/bin/sh\nsleep 1\n", executable=False)
    assert has_backend(str(d)) is False

    _script(str(d), "start.sh", "#!/bin/sh\nsleep 1\n")
    assert has_backend(str(d)) is True


def test_script_environment_excludes_the_servers_own(monkeypatch):
    """The server's environment holds the Octopus token, credentials and tunnel
    config. A backend has no business seeing any of it, and inheriting it
    wholesale is invisible until it isn't."""
    from server.app_backends import script_env

    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "super-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    env = script_env("a1", "/apps/demo", port=4100)

    assert "OCTOPUS_AUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["APP_DATA_DIR"] == "/apps/demo.data"
    assert env["APP_RUNTIME_DIR"] == "/apps/demo.runtime"
    assert env["PORT"] == "4100"


def test_data_and_runtime_are_siblings_not_subdirectories(tmp_path):
    """The data directory must survive a rebuild rewriting the code directory,
    which a subdirectory of it would not."""
    from server.applications import data_dir_for, runtime_dir_for

    app = str(tmp_path / "demo")
    assert data_dir_for(app) == str(tmp_path / "demo.data")
    assert runtime_dir_for(app) == str(tmp_path / "demo.runtime")
    for d in (data_dir_for(app), runtime_dir_for(app)):
        assert not d.startswith(app + os.sep)


@pytest.mark.asyncio
async def test_a_real_backend_answers_through_the_proxy(client):
    """The whole feature, end to end: an application ships `start.sh`, Octopus
    starts it, and `/apps/{id}/api/…` reaches it.

    The backend here is a real HTTP server in a real subprocess — a stub would
    prove the routing and none of the contract (port binding, foreground
    execution, readiness, teardown).
    """
    from server.app_backends import backend_supervisor

    app_row = await _api_create(client, name="With Backend")
    app_dir = app_row["app_dir"]
    Path(app_dir, "index.html").write_text("<html>ui</html>")
    _script(
        app_dir,
        "start.sh",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "cat > \"$APP_RUNTIME_DIR/serve.py\" <<'EOF'\n"
        "import json, os\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        body = json.dumps({'path': self.path,\n"
        "                           'data_dir': os.environ['APP_DATA_DIR']}).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('content-type', 'application/json')\n"
        "        self.end_headers()\n"
        "        self.wfile.write(body)\n"
        "    def log_message(self, *a):\n"
        "        pass\n"
        "HTTPServer(('127.0.0.1', int(os.environ['PORT'])), H).serve_forever()\n"
        "EOF\n"
        'exec python3 "$APP_RUNTIME_DIR/serve.py"\n',
    )
    os.makedirs(Path(app_dir + ".runtime"), exist_ok=True)

    try:
        resp = await client.get(
            f"/apps/{app_row['id']}/api/notes?limit=2", headers=HEADERS
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        # The prefix is preserved, so a backend routes on the path it declared.
        assert payload["path"] == "/api/notes?limit=2"
        # And it ran with the data directory the contract promises.
        assert payload["data_dir"] == app_dir + ".data"

        # Static files still come from the code directory, unproxied.
        page = await client.get(f"/apps/{app_row['id']}/index.html", headers=HEADERS)
        assert page.status_code == 200
        assert "ui" in page.text

        # The row reports the backend as running, with its port.
        detail = (await client.get(
            f"/api/applications/{app_row['id']}", headers=HEADERS
        )).json()
        assert detail["backend"]["state"] == "running"
        assert detail["backend"]["port"]
    finally:
        await backend_supervisor.stop_all()


@pytest.mark.asyncio
async def test_proxy_is_404_when_the_app_has_no_backend(client):
    app_row = await _api_create(client, name="Static Only")
    Path(app_row["app_dir"], "index.html").write_text("<html></html>")
    resp = await client.get(f"/apps/{app_row['id']}/api/anything", headers=HEADERS)
    assert resp.status_code == 404
    assert "no backend" in resp.text.lower()


@pytest.mark.asyncio
async def test_proxy_reports_a_broken_backend_rather_than_hanging(client):
    """A backend that exits immediately is a 503 naming the reason, not a 500
    and not a wait — "it doesn't work" with no output is the failure this
    feature exists to avoid."""
    from server.app_backends import backend_supervisor

    app_row = await _api_create(client, name="Broken Backend")
    _script(app_row["app_dir"], "start.sh", "#!/bin/sh\necho 'boom' >&2\nexit 7\n")
    try:
        resp = await client.get(f"/apps/{app_row['id']}/api/x", headers=HEADERS)
        assert resp.status_code == 503
        assert "exited 7" in resp.text

        detail = (await client.get(
            f"/api/applications/{app_row['id']}", headers=HEADERS
        )).json()
        assert detail["backend"]["state"] == "failed"
        assert any("boom" in line for line in detail["backend"]["log_tail"])
    finally:
        await backend_supervisor.stop_all()


@pytest.mark.asyncio
async def test_proxy_requires_auth(client):
    app_row = await _api_create(client, name="Guarded")
    _script(app_row["app_dir"], "start.sh", "#!/bin/sh\nsleep 5\n")
    resp = await client.get(f"/apps/{app_row['id']}/api/x")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_deleting_an_application_stops_its_backend(client):
    """A running server must not outlive its application. Nothing points at it
    afterwards, so neither the reaper nor shutdown could ever reach it — it
    would keep its port and hold files open under a deleted directory."""
    from server.app_backends import RUNNING, backend_supervisor

    app_row = await _api_create(client, name="Doomed Backend")
    _script(
        app_row["app_dir"],
        "start.sh",
        '#!/usr/bin/env bash\nexec python3 -m http.server "$PORT" --bind 127.0.0.1\n',
    )
    try:
        resp = await client.get(f"/apps/{app_row['id']}/api/x", headers=HEADERS)
        assert resp.status_code in (200, 404)  # the stub server's own answer
        state = backend_supervisor.get(app_row["id"])
        assert state and state.state == RUNNING
        proc = state.process
        assert proc is not None and proc.returncode is None

        await client.delete(f"/api/applications/{app_row['id']}", headers=HEADERS)

        await asyncio.sleep(0.3)
        assert proc.returncode is not None, "the backend process outlived its app"
    finally:
        await backend_supervisor.stop_all()


@pytest.mark.asyncio
async def test_deleting_an_application_removes_its_data_and_runtime(client):
    """The data and runtime directories are siblings, so removing the code
    directory leaves them behind unless we say otherwise."""
    app_row = await _api_create(client, name="Sibling Cleanup")
    app_dir = app_row["app_dir"]
    Path(app_dir, "index.html").write_text("<html></html>")
    os.makedirs(app_dir + ".data", exist_ok=True)
    os.makedirs(app_dir + ".runtime", exist_ok=True)
    Path(app_dir + ".data", "notes.json").write_text("[]")

    await client.delete(f"/api/applications/{app_row['id']}", headers=HEADERS)

    assert not os.path.exists(app_dir)
    assert not os.path.exists(app_dir + ".data")
    assert not os.path.exists(app_dir + ".runtime")


def test_build_prompt_states_the_home_screen_icon_rules():
    """The four rules an agent gets wrong otherwise. Each one produces a
    silently broken icon rather than an error, which is why they're spelled out
    rather than left to "ship an icon"."""
    prompt = ApplicationManager.compose_build_prompt(
        name="X", description="d", app_dir="/tmp/x"
    )
    assert "apple-touch-icon.png" in prompt
    assert "PNG" in prompt and "SVG" in prompt      # Safari ignores SVG here
    assert "opaque" in prompt                        # alpha composites to black
    assert "180x180" in prompt
    assert "full-bleed" in prompt                    # iOS applies its own mask
    # And the trap specific to us: apps live under /apps/<id>/, so an absolute
    # href resolves against Octopus instead of the app.
    assert "RELATIVE" in prompt


def test_apple_touch_icon_is_discovered_for_the_sidebar_too(tmp_path):
    """The same file doubles as the sidebar icon when an app ships nothing
    else — it's already in the candidate list, ahead of favicon.ico."""
    from server.applications import discover_icon_src

    d = tmp_path / "app"
    d.mkdir()
    (d / "index.html").write_text("<html></html>")
    (d / "apple-touch-icon.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (d / "favicon.ico").write_bytes(b"\x00")
    assert discover_icon_src(str(d), "index.html") == "apple-touch-icon.png"
