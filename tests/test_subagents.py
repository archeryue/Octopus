"""Native sub-agents, normalized (docs/plans/native-subagents.md).

Both CLIs fan work out to short-lived helpers inside a turn and narrate it
while it runs — Claude Code through `system/task_*` events around a `Task`
call, Codex through `collab_tool_call` items. These tests pin the
normalization (one shape for both), the session-level bookkeeping (live UI
state, never persisted) and the `--agents` rendering that lets an Octopus
agent bring sub-agents of its own.

The event payloads below are copied from real CLI runs.
"""

from __future__ import annotations

import json

import pytest

from server.harness import SubagentUpdate
from server.harness.claude_code import (
    ClaudeEventParser,
    _render_subagents,
    build_turn_argv,
)
from server.harness.codex import CodexEventParser
from server.harness.profile import TurnContext

TOOL_USE_ID = "toolu_01Hgbnw2SWBPzZFH8wJyEZZT"
TASK_ID = "ad54ad25029726077"


def _ctx(**over) -> TurnContext:
    base = dict(
        prompt="hi",
        working_dir="/tmp",
        resume_id=None,
        system_prompt="persona",
        model=None,
        tool_allow=None,
        tool_deny=None,
        mcp_servers=[],
        credential=None,
    )
    base.update(over)
    return TurnContext(**base)


def _subagent_events(parser, objs) -> list[SubagentUpdate]:
    out: list[SubagentUpdate] = []
    for obj in objs:
        out += [
            e.subagent for e in parser.parse(obj).events if e.type == "subagent"
        ]
    return out


# --------------------------------------------------------------- claude code

CLAUDE_RUN = [
    {
        "type": "system",
        "subtype": "task_started",
        "task_id": TASK_ID,
        "tool_use_id": TOOL_USE_ID,
        "description": "Find MARKER_ZX9 token",
        "subagent_type": "Explore",
        "is_backgrounded": False,
        "spawn_depth": 1,
        "task_type": "local_agent",
        "prompt": "Search the working directory for MARKER_ZX9.",
    },
    {
        "type": "system",
        "subtype": "task_progress",
        "task_id": TASK_ID,
        "tool_use_id": TOOL_USE_ID,
        "description": "Running Search recursively for the token",
        "subagent_type": "Explore",
        "usage": {"total_tokens": 11843, "tool_uses": 1, "duration_ms": 1332},
        "last_tool_name": "Bash",
    },
    # The anonymous one: a status patch with no tool_use_id and no name.
    {
        "type": "system",
        "subtype": "task_updated",
        "task_id": TASK_ID,
        "patch": {"status": "completed", "end_time": 1789613746921},
    },
    {
        "type": "system",
        "subtype": "task_notification",
        "task_id": TASK_ID,
        "tool_use_id": TOOL_USE_ID,
        "status": "completed",
        "summary": "Found it in notes.txt",
        "usage": {"total_tokens": 13825, "tool_uses": 3, "duration_ms": 6100},
    },
]


def test_claude_task_events_normalize_into_one_run():
    updates = _subagent_events(ClaudeEventParser(), CLAUDE_RUN)
    assert [u.status for u in updates] == [
        "running",
        "running",
        "completed",
        "completed",
    ]
    # Every observation names the same run, including the status patch that
    # arrives with only a task_id — the one event that can't identify itself.
    assert {u.task_id for u in updates} == {TASK_ID}
    assert {u.tool_use_id for u in updates} == {TOOL_USE_ID}
    assert {u.name for u in updates} == {"Explore"}

    started, progress, _patch, done = updates
    assert started.prompt.startswith("Search the working directory")
    assert progress.description == "Running Search recursively for the token"
    assert (progress.tokens, progress.tool_uses) == (11843, 1)
    # The brief is carried only on the first observation; repeating it on
    # every progress tick would put a wall of text on the wire 20 times.
    assert progress.prompt == ""
    assert done.summary == "Found it in notes.txt"


def test_claude_unknown_task_status_is_still_running():
    (update,) = _subagent_events(
        ClaudeEventParser(),
        [
            {
                "type": "system",
                "subtype": "task_progress",
                "task_id": "t1",
                "tool_use_id": "tu1",
                "status": "whatever-comes-next",
            }
        ],
    )
    assert update.status == "running"


def test_claude_task_map_is_bounded():
    """A held process parses many turns; the name map can't grow forever."""
    parser = ClaudeEventParser()
    for i in range(200):
        parser.parse(
            {
                "type": "system",
                "subtype": "task_started",
                "task_id": f"task-{i}",
                "tool_use_id": f"tu-{i}",
                "subagent_type": "Explore",
            }
        )
    assert len(parser._tasks) <= 64


def test_claude_events_without_a_task_id_are_ignored():
    assert _subagent_events(
        ClaudeEventParser(), [{"type": "system", "subtype": "task_progress"}]
    ) == []


# ---------------------------------------------------------------- the argv


def test_agents_json_is_rendered_only_when_defined():
    argv, _ = build_turn_argv(_ctx())
    assert "--agents" not in argv

    argv, _ = build_turn_argv(
        _ctx(
            subagents=[
                {
                    "name": "reviewer",
                    "description": "Reviews code",
                    "prompt": "You review.",
                    "model": "sonnet",
                    "tools": ["Read", "Grep"],
                },
                # Nameless drafts are dropped rather than sent as "".
                {"name": "   ", "description": "unfinished"},
            ]
        )
    )
    spec = json.loads(argv[argv.index("--agents") + 1])
    assert list(spec) == ["reviewer"]
    assert spec["reviewer"] == {
        "description": "Reviews code",
        "prompt": "You review.",
        "tools": ["Read", "Grep"],
        "model": "sonnet",
    }


def test_agents_json_omits_empty_optionals():
    # An explicit blank would override the CLI's own defaults; absence
    # doesn't.
    spec = _render_subagents([{"name": "a", "description": "d", "prompt": "p"}])
    assert spec == {"a": {"description": "d", "prompt": "p"}}
    assert _render_subagents(["not-a-dict", {"description": "no name"}]) == {}


# --------------------------------------------------------------------- codex

CODEX_RUN = [
    {
        "type": "item.started",
        "item": {
            "id": "item_0",
            "type": "collab_tool_call",
            "tool": "spawn_agent",
            "sender_thread_id": "parent",
            "receiver_thread_ids": [],
            "prompt": "Find MARKER_ZX9.",
            "agents_states": {},
            "status": "in_progress",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "item_0",
            "type": "collab_tool_call",
            "tool": "spawn_agent",
            "sender_thread_id": "parent",
            "receiver_thread_ids": ["01a0ad62-b228-74d1-a2c3-1328858cca57"],
            "prompt": "Find MARKER_ZX9.",
            "agents_states": {
                "01a0ad62-b228-74d1-a2c3-1328858cca57": {
                    "status": "pending_init",
                    "message": None,
                }
            },
            "status": "completed",
        },
    },
    {
        "type": "item.started",
        "item": {
            "id": "item_1",
            "type": "collab_tool_call",
            "tool": "wait",
            "sender_thread_id": "parent",
            "receiver_thread_ids": ["01a0ad62-b228-74d1-a2c3-1328858cca57"],
            "prompt": None,
            "agents_states": {},
            "status": "in_progress",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "item_1",
            "type": "collab_tool_call",
            "tool": "wait",
            "sender_thread_id": "parent",
            "receiver_thread_ids": ["01a0ad62-b228-74d1-a2c3-1328858cca57"],
            "prompt": None,
            "agents_states": {
                "01a0ad62-b228-74d1-a2c3-1328858cca57": {
                    "status": "completed",
                    "message": "./notes.txt",
                }
            },
            "status": "completed",
        },
    },
]


def test_codex_collab_calls_track_one_sub_agent_across_items():
    parser = CodexEventParser()
    updates = _subagent_events(parser, CODEX_RUN)

    # `spawn_agent` and `wait` are separate tool calls describing the SAME
    # sub-agent, so they must land on one card — keyed by the spawn.
    assert {u.task_id for u in updates} == {"item_0"}
    assert [u.status for u in updates] == [
        "running",
        "running",
        "running",
        "completed",
    ]
    assert updates[0].prompt == "Find MARKER_ZX9."
    assert updates[-1].name == "agent 01a0ad62"
    assert updates[-1].summary == "./notes.txt"


def test_codex_collab_calls_are_also_real_tool_calls():
    """Claude's `Task` is a tool call in the transcript; Codex's equivalent
    has to be one too, or the card has nothing to attach to."""
    parser = CodexEventParser()
    events = [e for obj in CODEX_RUN for e in parser.parse(obj).events]
    kinds = [(e.type, e.tool_name, e.tool_use_id) for e in events if e.type in ("tool_use", "tool_result")]
    assert ("tool_use", "subagent:spawn_agent", "item_0") in kinds
    assert ("tool_use", "subagent:wait", "item_1") in kinds
    results = [e for e in events if e.type == "tool_result"]
    assert results[-1].content == "./notes.txt"


def test_codex_owner_map_is_bounded():
    parser = CodexEventParser()
    for i in range(200):
        parser.parse(
            {
                "type": "item.completed",
                "item": {
                    "id": f"item_{i}",
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "receiver_thread_ids": [f"thread-{i}"],
                    "agents_states": {},
                },
            }
        )
    assert len(parser._collab_owner) <= 64


# ------------------------------------------------------- session bookkeeping


def _session():
    from server.session_manager import Session

    return Session(id="s1", name="s", working_dir="/tmp")


def test_observations_merge_onto_the_run():
    """Every harness reports sub-agents in partial shapes — a status patch
    with no name, a summary with no counters. The card needs the union."""
    from server.session_manager import SessionManager

    session = _session()
    SessionManager._record_subagent(
        session,
        SubagentUpdate(
            task_id="t1",
            tool_use_id="tu1",
            name="Explore",
            description="searching",
            prompt="find it",
            tokens=100,
        ),
    )
    SessionManager._record_subagent(
        session,
        SubagentUpdate(task_id="t1", tool_use_id="tu1", status="completed", summary="done"),
    )

    (run,) = session._subagents.values()
    assert run.status == "completed"
    assert run.summary == "done"
    # Carried forward rather than blanked by the partial update.
    assert run.name == "Explore"
    assert run.description == "searching"
    assert run.prompt == "find it"
    assert run.tokens == 100


def test_runs_are_bounded_per_session():
    from server.session_manager import SessionManager, _MAX_SUBAGENTS_PER_SESSION

    session = _session()
    for i in range(_MAX_SUBAGENTS_PER_SESSION + 20):
        SessionManager._record_subagent(
            session, SubagentUpdate(task_id=f"t{i}", tool_use_id=f"tu{i}")
        )
    assert len(session._subagents) == _MAX_SUBAGENTS_PER_SESSION
    # Oldest dropped, newest kept.
    assert "tu0" not in session._subagents
    assert f"tu{_MAX_SUBAGENTS_PER_SESSION + 19}" in session._subagents


def test_an_observation_without_a_key_is_ignored():
    from server.session_manager import SessionManager

    session = _session()
    SessionManager._record_subagent(session, SubagentUpdate(task_id="", tool_use_id=None))
    assert session._subagents == {}


def test_subagent_events_broadcast_but_never_persist():
    from server.harness import HarnessEvent
    from server.session_manager import SessionManager

    event = HarnessEvent(
        type="subagent",
        subagent=SubagentUpdate(
            task_id="t1",
            tool_use_id="tu1",
            status="running",
            name="Explore",
            description="searching",
            tokens=11843,
            tool_uses=1,
        ),
    )
    # The durable record is the Task tool call already in the transcript;
    # persisting progress would double every sub-agent in the history.
    assert SessionManager._event_to_message_content(event) is None

    ws = SessionManager._event_to_ws_message("s1", event)
    assert ws == {
        "type": "subagent",
        "session_id": "s1",
        "task_id": "t1",
        "tool_use_id": "tu1",
        "status": "running",
        "name": "Explore",
        "description": "searching",
        "prompt": "",
        "summary": "",
        "tokens": 11843,
        "tool_uses": 1,
        "duration_ms": None,
    }


# ------------------------------------------------------------------ the API


@pytest.fixture
async def client():
    from httpx import ASGITransport, AsyncClient

    from server.agent_manager import AgentManager
    from server.database import Database
    from server.main import app
    from server.routers import agents as agents_mod
    from server.session_manager import session_manager

    db = Database(":memory:")
    await db.initialize()
    session_manager.sessions.clear()
    await session_manager.initialize(db)
    agents_mod.set_manager(AgentManager(db))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await db.close()


HEADERS = {"Authorization": "Bearer changeme"}


@pytest.mark.asyncio
async def test_an_agent_can_carry_its_own_subagents(client):
    created = await client.post(
        "/api/agents",
        json={
            "name": "Builder",
            "subagents": [
                {
                    "name": "reviewer",
                    "description": "Reviews code",
                    "prompt": "You review.",
                }
            ],
        },
        headers=HEADERS,
    )
    assert created.status_code == 201, created.text
    agent = created.json()
    assert [sa["name"] for sa in agent["subagents"]] == ["reviewer"]

    # And they round-trip through an edit, where an agent that defines none
    # is the normal state.
    patched = await client.patch(
        f"/api/agents/{agent['id']}", json={"subagents": []}, headers=HEADERS
    )
    assert patched.status_code == 200
    assert patched.json()["subagents"] == []


@pytest.mark.asyncio
async def test_pre_existing_agents_read_back_with_no_subagents(client):
    """The column is additive — every row that predates it means "built-ins
    only", not "broken agent"."""
    listed = (await client.get("/api/agents", headers=HEADERS)).json()
    assert listed, "the Default Agent should exist"
    assert all(a["subagents"] == [] for a in listed)


@pytest.mark.asyncio
async def test_the_session_snapshot_carries_live_runs(client):
    from server.session_manager import SessionManager, session_manager

    agents = (await client.get("/api/agents", headers=HEADERS)).json()
    made = await client.post(
        "/api/sessions",
        json={"name": "S", "working_dir": "/tmp", "agent_id": agents[0]["id"]},
        headers=HEADERS,
    )
    sid = made.json()["id"]

    SessionManager._record_subagent(
        session_manager.sessions[sid],
        SubagentUpdate(
            task_id="t1", tool_use_id="tu1", name="Explore", description="searching"
        ),
    )

    # A browser reloading mid-run repaints the card instead of showing a
    # tool call that looks frozen.
    detail = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()
    assert detail["subagents"] == [
        {
            "task_id": "t1",
            "tool_use_id": "tu1",
            "status": "running",
            "name": "Explore",
            "description": "searching",
            "prompt": "",
            "summary": "",
            "tokens": None,
            "tool_uses": None,
            "duration_ms": None,
        }
    ]


# ------------------------------------------- work that outlives its own turn


@pytest.mark.asyncio
async def test_events_after_a_turn_reach_the_session(client):
    """The CLI's async sub-agents are the reason this exists.

    `Async agent launched successfully` returns immediately: the turn ends,
    the sub-agent keeps working inside the held process, and when it lands the
    CLI wakes the agent to report it. Those events used to be dropped — the
    card spun forever and the answer never arrived (native-subagents.md §7).
    """
    from server.harness import HarnessEvent
    from server.session_manager import session_manager

    agents = (await client.get("/api/agents", headers=HEADERS)).json()
    made = await client.post(
        "/api/sessions",
        json={"name": "Async", "working_dir": "/tmp", "agent_id": agents[0]["id"]},
        headers=HEADERS,
    )
    sid = made.json()["id"]

    broadcast: list[dict] = []
    session_manager.on_broadcast("test", lambda m: _collect(broadcast, m))
    try:
        # The sub-agent finishes…
        await session_manager._handle_idle_event(
            sid,
            HarnessEvent(
                type="subagent",
                subagent=SubagentUpdate(
                    task_id="t1",
                    tool_use_id="tu1",
                    status="completed",
                    name="general-purpose",
                    summary="2 files",
                ),
            ),
        )
        # …and the agent reports it in a turn nobody asked for.
        await session_manager._handle_idle_event(
            sid, HarnessEvent(type="text", content="The background agent found 2 files.")
        )
    finally:
        session_manager.remove_broadcast("test")

    kinds = [m["type"] for m in broadcast]
    assert "subagent" in kinds and "assistant_text" in kinds

    # The report is persisted, so it's still there after a reload — unlike
    # the progress that produced it.
    detail = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()
    assert any(
        m["type"] == "text" and "background agent found 2 files" in (m["content"] or "")
        for m in detail["messages"]
    )
    assert detail["subagents"][0]["status"] == "completed"


async def _collect(sink: list[dict], message: dict) -> None:
    sink.append(message)


@pytest.mark.asyncio
async def test_an_idle_event_for_an_unknown_session_is_dropped():
    from server.harness import HarnessEvent
    from server.session_manager import session_manager

    # Deleted while its process was still finishing something.
    await session_manager._handle_idle_event(
        "gone", HarnessEvent(type="text", content="hello?")
    )


def test_losing_the_process_ends_its_sub_agents():
    """A sub-agent lives inside the CLI process. When the reaper stops it, a
    card that keeps spinning is a lie."""
    from server.session_manager import SessionManager

    mgr = SessionManager()
    session = _session()
    SessionManager._record_subagent(
        session, SubagentUpdate(task_id="t1", tool_use_id="tu1", name="Explore")
    )
    SessionManager._record_subagent(
        session,
        SubagentUpdate(
            task_id="t2", tool_use_id="tu2", status="completed", summary="done"
        ),
    )
    session._held_run_at = 1.0

    mgr._forget_backend(session)

    assert session._backend is None and session._held_run_at is None
    assert session._subagents["tu1"].status == "failed"
    assert "interrupted" in session._subagents["tu1"].description
    # A run that already finished keeps its result.
    assert session._subagents["tu2"].status == "completed"
    assert session._subagents["tu2"].summary == "done"


@pytest.mark.asyncio
async def test_the_run_hands_post_turn_events_to_the_idle_handler():
    """Below the session layer: once a turn's stream is closed, further
    events go to the handler instead of the floor."""
    from server.harness import HarnessEvent
    from server.harness.run import HarnessRun
    from server.harness.registry import get_harness

    run = get_harness("claude-code").create_run()
    seen: list[HarnessEvent] = []

    async def handler(event: HarnessEvent) -> None:
        seen.append(event)

    run.set_idle_handler(handler)

    # An in-turn text lands on the stream…
    await run._handle_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "in turn"}]},
            }
        )
    )
    assert seen == []
    assert isinstance(run, HarnessRun)

    # …the turn ends…
    run._close_stream()

    # …and what comes after belongs to the session.
    await run._handle_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "after the turn"}]},
            }
        )
    )
    assert [e.content for e in seen] == ["after the turn"]
