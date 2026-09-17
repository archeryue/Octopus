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

from tests.cli_gate import claude_cli_works  # noqa: E402

from server.harness import HarnessEvent, RunConfig, get_harness  # noqa: E402

pytestmark = pytest.mark.skipif(
    not claude_cli_works(),
    reason="claude CLI unavailable or not signed in; skip real-CLI tests",
)

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
