"""Per-agent native-memory provisioning (docs/plans/memory.md §6).

Pure path derivation + idempotent provisioning of the one canonical per-agent
memory dir. Memory is decoupled from both harnesses' config/auth dirs, so there
is no symlink/auth machinery to test here; the real read/write cycle is covered
by the gated real-CLI test.
"""

from __future__ import annotations

import pytest

from server import agent_memory
from server.config import settings


@pytest.fixture
def agents_root(tmp_path, monkeypatch):
    root = tmp_path / "agents"
    monkeypatch.setattr(settings, "agents_dir", str(root))
    return root


def test_path_derivation(agents_root):
    assert agent_memory.agent_state_dir("a1") == agents_root / "a1"
    assert agent_memory.agent_memory_dir("a1") == agents_root / "a1" / "memory"


def test_ensure_and_remove_agent_dirs(agents_root):
    agent_memory.ensure_agent_dirs("a")
    assert agent_memory.agent_memory_dir("a").is_dir()

    agent_memory.remove_agent_dir("a")
    assert not agent_memory.agent_state_dir("a").exists()


def test_ensure_agent_dirs_is_idempotent(agents_root):
    agent_memory.ensure_agent_dirs("a")
    # Drop a file in, re-ensure, confirm it survives (no clobber).
    fact = agent_memory.agent_memory_dir("a") / "MEMORY.md"
    fact.write_text("- [x](x.md) — y")
    agent_memory.ensure_agent_dirs("a")
    assert fact.read_text() == "- [x](x.md) — y"


def test_uses_configured_agents_dir(tmp_path, monkeypatch):
    """`~` / settings override is honored at call time."""
    monkeypatch.setattr(settings, "agents_dir", "~/.octopus/agents")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert agent_memory.agent_memory_dir("z") == tmp_path / ".octopus/agents/z/memory"


@pytest.mark.asyncio
async def test_agent_manager_provisions_and_cleans_dirs(agents_root, tmp_path):
    """create_agent provisions the per-agent memory dir; hard delete removes it."""
    from server.agent_manager import AgentManager
    from server.database import Database

    db = Database(str(tmp_path / "t.db"))
    await db.initialize()
    try:
        mgr = AgentManager(db)
        agent = await mgr.create_agent(name="Mem Agent")
        aid = agent["id"]
        assert agent_memory.agent_memory_dir(aid).is_dir()

        await mgr.delete_agent(aid)
        assert not agent_memory.agent_state_dir(aid).exists()
    finally:
        await db.close()
