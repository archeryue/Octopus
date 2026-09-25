"""A REAL sub-agent run, end to end (docs/plans/native-subagents.md).

The unit suite pins the normalization against captured payloads. This proves
the contract still holds against the installed CLI: a turn that spawns a
`Task` produces `subagent` events that name the run, say what it is doing,
count its work and carry its summary — and that a sub-agent an Octopus agent
brings with it (`--agents`) is actually registered.

Costs real API calls; auto-skipped when `claude` isn't installed or signed in.
"""

from __future__ import annotations

import asyncio
import glob
import os

import pytest

for _d in [
    os.path.expanduser("~/.local/bin"),
    "/usr/local/bin",
    *sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin"))),
]:
    if _d and _d not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")


from server.harness import HarnessEvent, RunConfig, get_harness  # noqa: E402

pytestmark = [pytest.mark.real, pytest.mark.real_claude]

MARKER = "MARKER_ZX9"


async def _drain(run, timeout: float = 240.0) -> list[HarnessEvent]:
    events: list[HarnessEvent] = []

    async def collect() -> None:
        async for event in run.stream():
            events.append(event)

    await asyncio.wait_for(collect(), timeout=timeout)
    return events


@pytest.mark.asyncio
async def test_a_real_task_streams_normalized_subagent_events(tmp_path):
    (tmp_path / "notes.txt").write_text(f"alpha beta {MARKER} gamma\n")
    (tmp_path / "other.txt").write_text("nothing here\n")

    run = get_harness("claude-code").create_run(RunConfig())
    await run.start(
        "Use the Task tool with subagent_type \"Explore\" to find which file "
        f"in this directory contains the token {MARKER}. Report just the "
        "filename.",
        str(tmp_path),
    )
    try:
        events = await _drain(run)
    finally:
        await run.stop()

    updates = [e.subagent for e in events if e.type == "subagent"]
    assert updates, "no sub-agent events — the Task contract changed"

    # It is one run, identified the whole way through: every observation
    # points at the same task and the same parent tool call.
    assert len({u.task_id for u in updates}) == 1
    assert len({u.tool_use_id for u in updates if u.tool_use_id}) == 1
    assert {u.name for u in updates if u.name} == {"Explore"}

    # It reported progress while working…
    assert any(u.status == "running" and u.description for u in updates)
    assert any((u.tool_uses or 0) >= 1 for u in updates)
    # …and finished, with its answer.
    assert updates[-1].status == "completed"
    summary = " ".join(u.summary for u in updates if u.summary)
    assert "notes.txt" in summary

    # The durable half is unchanged: the spawning tool call and its result
    # are in the transcript, which is why progress is never persisted. The
    # CLI has renamed this tool across versions (`Task` → `Agent`), which is
    # exactly why the UI keys the card on tool_use_id rather than the name.
    spawn_calls = [
        e for e in events if e.type == "tool_use" and e.tool_name in ("Task", "Agent")
    ]
    assert spawn_calls, [e.tool_name for e in events if e.type == "tool_use"]
    assert spawn_calls[0].tool_use_id == updates[0].tool_use_id


@pytest.mark.asyncio
async def test_an_agents_own_subagent_is_registered(tmp_path):
    """`--agents` definitions reach the CLI: the model can address one by the
    name its Octopus agent gave it."""
    run = get_harness("claude-code").create_run(
        RunConfig(
            subagents=[
                {
                    "name": "octopus-probe",
                    "description": "Answers with a fixed codeword.",
                    "prompt": (
                        "You always reply with exactly the word PERIWINKLE "
                        "and nothing else."
                    ),
                }
            ]
        )
    )
    await run.start(
        "Use the Task tool with subagent_type \"octopus-probe\" and the "
        "prompt \"say the word\". Then tell me exactly what it replied.",
        str(tmp_path),
    )
    try:
        events = await _drain(run)
    finally:
        await run.stop()

    names = {u.name for u in (e.subagent for e in events if e.type == "subagent")}
    assert "octopus-probe" in names, names
    text = " ".join(e.content or "" for e in events if e.type == "text")
    assert "PERIWINKLE" in text


@pytest.mark.asyncio
async def test_an_async_subagent_finishes_after_its_turn_ends(tmp_path):
    """The bug this whole out-of-turn path exists for.

    Claude Code decides on its own to run some sub-agents asynchronously:
    the tool returns "Async agent launched successfully", the turn ends, and
    the work continues inside the held process. Everything after that —
    the sub-agent's completion AND the turn the CLI wakes to report it —
    used to be dropped on the floor (native-subagents.md §7).
    """
    (tmp_path / "a.txt").write_text("one\n")
    (tmp_path / "b.txt").write_text("two\n")

    run = get_harness("claude-code").create_run(RunConfig())
    after_turn: list[HarnessEvent] = []
    done = asyncio.Event()

    async def idle(event: HarnessEvent) -> None:
        after_turn.append(event)
        # The report the agent makes once the sub-agent lands ends with its
        # own result event.
        if event.type == "result":
            done.set()

    run.set_idle_handler(idle)
    await run.start(
        "Launch an ASYNCHRONOUS background sub-agent (Task/Agent tool, "
        "subagent_type general-purpose) that counts the .txt files in this "
        "directory and reports the number. Do not wait for it — end your turn "
        "immediately after launching it.",
        str(tmp_path),
    )
    try:
        await _drain(run, timeout=180.0)          # the turn itself
        await asyncio.wait_for(done.wait(), timeout=180.0)  # what came after
    finally:
        await run.stop()

    # The sub-agent completed, out of turn.
    updates = [e.subagent for e in after_turn if e.type == "subagent"]
    assert updates, [e.type for e in after_turn]
    assert any(u.status == "completed" for u in updates)

    # And the agent's follow-up report — the answer the user is waiting for —
    # arrived as ordinary session text rather than vanishing.
    text = " ".join(e.content or "" for e in after_turn if e.type == "text")
    assert "2" in text, text
