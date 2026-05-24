"""Phase A — per-agent native-memory provisioning (docs/plans/memory.md §6).

Pure path derivation + idempotent filesystem provisioning. No CLI here; the
real read/write cycle is exercised by the gated real-CLI test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from server import agent_memory
from server.config import settings


@pytest.fixture
def agents_root(tmp_path, monkeypatch):
    """Point the per-agent state root at a temp dir and $HOME at another, so
    `~`-based host lookups (Claude creds) are controlled too."""
    root = tmp_path / "agents"
    monkeypatch.setattr(settings, "agents_dir", str(root))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return root


# ------------------------------------------------------------------ paths


def test_path_derivation(agents_root):
    d = agent_memory.agent_state_dir("agent-1")
    assert d == agents_root / "agent-1"
    assert agent_memory.agent_memory_dir("agent-1") == agents_root / "agent-1" / "memory"
    assert agent_memory.agent_claude_home("agent-1") == agents_root / "agent-1" / "claude-home"


def test_cwd_slug_matches_claude_encoding():
    # Verified against the real CLI: every non-alphanumeric char → '-'.
    assert agent_memory.cwd_slug("/home/start-up/Octopus") == "-home-start-up-Octopus"
    # Dots and underscores also collapse to '-'.
    assert agent_memory.cwd_slug("/tmp/oct.mem_test.x_y") == "-tmp-oct-mem-test-x-y"


# ------------------------------------------------------------------ provisioning


def test_ensure_and_remove_agent_dirs(agents_root):
    agent_memory.ensure_agent_dirs("a")
    assert agent_memory.agent_memory_dir("a").is_dir()
    assert agent_memory.agent_claude_home("a").is_dir()

    agent_memory.remove_agent_dir("a")
    assert not agent_memory.agent_state_dir("a").exists()


def test_ensure_memory_symlink_creates_and_is_idempotent(agents_root, tmp_path):
    claude_home = agent_memory.agent_claude_home("a")
    mem = agent_memory.agent_memory_dir("a")
    wd = str(tmp_path / "proj")

    agent_memory.ensure_memory_symlink(claude_home, wd, mem)
    link = claude_home / "projects" / agent_memory.cwd_slug(wd) / "memory"
    assert link.is_symlink()
    assert os.readlink(link) == str(mem)
    assert mem.is_dir()  # canonical target created

    # Second call: no-op, link unchanged.
    agent_memory.ensure_memory_symlink(claude_home, wd, mem)
    assert os.readlink(link) == str(mem)


def test_ensure_memory_symlink_repairs_stale_link(agents_root, tmp_path):
    claude_home = agent_memory.agent_claude_home("a")
    mem = agent_memory.agent_memory_dir("a")
    wd = str(tmp_path / "proj")
    link = claude_home / "projects" / agent_memory.cwd_slug(wd) / "memory"
    link.parent.mkdir(parents=True)
    # A stale link pointing somewhere else.
    (tmp_path / "elsewhere").mkdir()
    link.symlink_to(tmp_path / "elsewhere")

    agent_memory.ensure_memory_symlink(claude_home, wd, mem)
    assert os.readlink(link) == str(mem)


def test_ensure_memory_symlink_replaces_squatting_dir(agents_root, tmp_path):
    claude_home = agent_memory.agent_claude_home("a")
    mem = agent_memory.agent_memory_dir("a")
    wd = str(tmp_path / "proj")
    link = claude_home / "projects" / agent_memory.cwd_slug(wd) / "memory"
    link.mkdir(parents=True)  # a real dir squatting the symlink spot
    (link / "stray.md").write_text("x")

    agent_memory.ensure_memory_symlink(claude_home, wd, mem)
    assert link.is_symlink()
    assert os.readlink(link) == str(mem)


# ------------------------------------------------------------------ claude auth


def test_ensure_claude_auth_skips_when_env_token(agents_root):
    home = agent_memory.agent_claude_home("a")
    home.mkdir(parents=True)
    # host has creds, but env token present → must NOT copy
    host_creds = Path(os.path.expanduser("~/.claude/.credentials.json"))
    host_creds.parent.mkdir(parents=True)
    host_creds.write_text('{"tok":"host"}')

    agent_memory.ensure_claude_auth(home, has_env_token=True)
    assert not (home / ".credentials.json").exists()


def test_ensure_claude_auth_copies_host_when_no_token(agents_root):
    home = agent_memory.agent_claude_home("a")
    home.mkdir(parents=True)
    host_creds = Path(os.path.expanduser("~/.claude/.credentials.json"))
    host_creds.parent.mkdir(parents=True)
    host_creds.write_text('{"tok":"host"}')

    agent_memory.ensure_claude_auth(home, has_env_token=False)
    dest = home / ".credentials.json"
    assert dest.exists() and dest.read_text() == '{"tok":"host"}'


def test_ensure_claude_auth_does_not_overwrite_existing(agents_root):
    home = agent_memory.agent_claude_home("a")
    home.mkdir(parents=True)
    (home / ".credentials.json").write_text('{"tok":"agent-own"}')
    host_creds = Path(os.path.expanduser("~/.claude/.credentials.json"))
    host_creds.parent.mkdir(parents=True)
    host_creds.write_text('{"tok":"host"}')

    agent_memory.ensure_claude_auth(home, has_env_token=False)
    assert (home / ".credentials.json").read_text() == '{"tok":"agent-own"}'


def test_ensure_claude_auth_noop_without_host_creds(agents_root):
    home = agent_memory.agent_claude_home("a")
    home.mkdir(parents=True)
    agent_memory.ensure_claude_auth(home, has_env_token=False)
    assert not (home / ".credentials.json").exists()


# ------------------------------------------------------------------ agent_manager


@pytest.mark.asyncio
async def test_agent_manager_provisions_and_cleans_dirs(agents_root, tmp_path):
    """create_agent provisions the per-agent dirs; hard delete removes them."""
    from server.agent_manager import AgentManager
    from server.database import Database

    db = Database(str(tmp_path / "t.db"))
    await db.initialize()
    try:
        mgr = AgentManager(db)
        agent = await mgr.create_agent(name="Mem Agent")
        aid = agent["id"]
        assert agent_memory.agent_memory_dir(aid).is_dir()
        assert agent_memory.agent_claude_home(aid).is_dir()

        await mgr.delete_agent(aid)
        assert not agent_memory.agent_state_dir(aid).exists()
    finally:
        await db.close()
