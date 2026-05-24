"""Real-CLI memory read-back (docs/plans/memory.md §6).

Proves the load-bearing guarantee — *each harness reads its per-agent memory
through the real wiring* — against the actual `claude` / `codex` binaries:
seed the canonical per-agent memory dir, then a fresh session must recall the
fact. (Claude reaches it via the per-agent CLAUDE_CONFIG_DIR + symlink that
`prepare_workspace` lays down; Codex via the injected blurb naming the dir.)

We assert the *read* path, not model-driven writes: whether a model chooses to
call its memory/file tools in a given turn is nondeterministic, but reading an
injected/instructed memory is reliable. Writes are covered by the unit tests
(wiring) + Claude's native behavior. Costs real API calls; auto-skipped when
the binary isn't on PATH, like the other real-CLI suites.
"""

from __future__ import annotations

import asyncio
import glob
import os
import shutil

import pytest

from server import agent_memory
from server.config import settings
from server.harness import HarnessEvent, RunConfig, get_harness

# Widen PATH so the CLIs resolve under non-interactive pytest (nvm/.local/bin).
for _d in [
    os.path.expanduser("~/.local/bin"),
    "/usr/local/bin",
    *sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin"))),
]:
    if _d and _d not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")

CODENAME = "BLUEOCTOPUS7723"


def _seed(agent_id: str) -> None:
    """Write a MEMORY.md index + one fact file into the canonical per-agent
    memory dir (Claude's native format — what both harnesses read)."""
    mem = agent_memory.agent_memory_dir(agent_id)
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "MEMORY.md").write_text(
        "# Memory Index\n\n- [User Profile](user-profile.md) — secret project codename\n"
    )
    (mem / "user-profile.md").write_text(
        "---\nname: user-profile\ndescription: secret project codename\n"
        "metadata:\n  type: user\n---\n\n"
        f"The user's secret project codename is {CODENAME}.\n"
    )


async def _drain(run, timeout: float = 150.0) -> list[HarnessEvent]:
    events: list[HarnessEvent] = []

    async def collect() -> None:
        async for ev in run.stream():
            events.append(ev)

    try:
        await asyncio.wait_for(collect(), timeout=timeout)
    except asyncio.TimeoutError:
        raise AssertionError(f"stream didn't end in {timeout}s: {[e.type for e in events]}")
    return events


def _text(events) -> str:
    return "\n".join(e.content for e in events if e.type == "text" and e.content)


async def _recall(backend: str, cfg: dict, wd: str) -> str:
    run = get_harness(backend).create_run(RunConfig(**cfg))
    await run.start(
        "Consult your long-term memory about me (read your MEMORY.md and any file "
        "it points to), then tell me my secret project codename. Answer with the "
        "codename, or UNKNOWN if your memory genuinely lacks it.",
        wd,
        None,
        None,
    )
    try:
        events = await _drain(run)
    finally:
        await run.stop()
    return _text(events)


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH")
@pytest.mark.asyncio
async def test_claude_reads_per_agent_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    aid = "memtest-claude"
    agent_memory.ensure_agent_dirs(aid)
    _seed(aid)
    wd = str(tmp_path / "ws")
    os.makedirs(wd)
    cfg = dict(
        model="haiku",
        memory_dir=str(agent_memory.agent_memory_dir(aid)),
        agent_config_dir=str(agent_memory.agent_claude_home(aid)),
    )
    out = await _recall("claude-code", cfg, wd)
    assert CODENAME in out.upper()


@pytest.mark.skipif(shutil.which("codex") is None, reason="codex CLI not on PATH")
@pytest.mark.asyncio
async def test_codex_reads_per_agent_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    aid = "memtest-codex"
    agent_memory.ensure_agent_dirs(aid)
    _seed(aid)
    wd = str(tmp_path / "ws")
    os.makedirs(wd)
    cfg = dict(memory_dir=str(agent_memory.agent_memory_dir(aid)))
    out = await _recall("codex", cfg, wd)
    assert CODENAME in out.upper()
