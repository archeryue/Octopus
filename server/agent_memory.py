"""Per-agent native-memory provisioning (docs/plans/memory.md).

Memory is each harness's NATIVE, agent-written markdown memory, persisted
and scoped per agent under ``<agents_dir>/<agent_id>/``. Both harnesses share
one canonical ``memory/`` directory:

- **Claude** runs with a per-agent ``CLAUDE_CONFIG_DIR`` (``claude-home/``),
  and its native memory path ``projects/<cwd-slug>/memory`` is symlinked to
  the canonical ``memory/`` dir — so Claude's built-in memory reads/writes the
  canonical dir. Auth comes from the env token; when none is attached we copy
  the host ``~/.claude/.credentials.json`` in once.
- **Codex** gets the canonical ``memory/`` path by absolute reference in its
  ``developer_instructions`` and reads/writes it with normal file tools.
  ``CODEX_HOME`` is unchanged (per-credential auth) — memory is decoupled from
  auth, so there is no per-agent auth-sync hazard.

Pure path helpers + idempotent filesystem provisioning; no global state. ``~``
is expanded at call time so tests that monkeypatch ``$HOME`` see the override.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ paths


def _agents_root() -> Path:
    return Path(os.path.expanduser(settings.agents_dir))


def agent_state_dir(agent_id: str) -> Path:
    """``<agents_dir>/<agent_id>/`` — the agent's durable state root."""
    return _agents_root() / agent_id


def agent_memory_dir(agent_id: str) -> Path:
    """The canonical per-agent memory dir (native markdown, shared by both
    harnesses)."""
    return agent_state_dir(agent_id) / "memory"


def agent_claude_home(agent_id: str) -> Path:
    """The per-agent ``CLAUDE_CONFIG_DIR``."""
    return agent_state_dir(agent_id) / "claude-home"


def cwd_slug(working_dir: str) -> str:
    """Encode an absolute working dir into Claude Code's project-folder name.

    Claude replaces every non-alphanumeric character with ``-`` (verified
    against the real CLI), e.g. ``/home/start-up/Octopus`` →
    ``-home-start-up-Octopus``. We resolve to absolute first so the slug
    matches what the CLI computes from the same cwd."""
    abs_wd = str(Path(working_dir).resolve())
    return re.sub(r"[^a-zA-Z0-9]", "-", abs_wd)


# ------------------------------------------------------------------ provisioning


def ensure_agent_dirs(agent_id: str) -> None:
    """Create the canonical ``memory/`` dir and ``claude-home/`` (idempotent)."""
    agent_memory_dir(agent_id).mkdir(parents=True, exist_ok=True)
    agent_claude_home(agent_id).mkdir(parents=True, exist_ok=True)


def remove_agent_dir(agent_id: str) -> None:
    """Delete the agent's entire state tree (memory + claude-home). Called on
    hard agent delete; archiving keeps the files."""
    shutil.rmtree(agent_state_dir(agent_id), ignore_errors=True)


def ensure_memory_symlink(
    claude_home: str | os.PathLike[str],
    working_dir: str,
    memory_dir: str | os.PathLike[str],
) -> None:
    """Point Claude's native memory path for ``working_dir`` at the canonical
    ``memory_dir``: ``<claude_home>/projects/<cwd-slug>/memory`` → ``memory_dir``.

    Idempotent; repairs a stale/wrong link or a squatting real dir. Best-effort
    (logs and returns on OSError) — a missing symlink degrades to Claude using
    an empty native memory, never a crash."""
    canonical = Path(memory_dir)
    canonical.mkdir(parents=True, exist_ok=True)
    project = Path(claude_home) / "projects" / cwd_slug(working_dir)
    link = project / "memory"
    try:
        project.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            if os.readlink(link) == str(canonical):
                return
            link.unlink()
        elif link.exists():
            if link.is_dir():
                shutil.rmtree(link)
            else:
                link.unlink()
        link.symlink_to(canonical, target_is_directory=True)
    except OSError:
        logger.warning("ensure_memory_symlink failed for %s", link, exc_info=True)


def ensure_claude_auth(
    claude_home: str | os.PathLike[str], has_env_token: bool
) -> None:
    """Seed a per-agent ``claude-home`` with the host login when the agent has
    no env-token credential, so Claude can authenticate under the custom
    ``CLAUDE_CONFIG_DIR``.

    No-op when an env token is present (it overrides the config dir), when the
    dir is already seeded, or when the host has no credentials file. The copy
    diverges from the host on later OAuth refresh — acceptable for the rare
    no-credential agent on a single-user host (docs/plans/memory.md §5)."""
    if has_env_token:
        return
    dest = Path(claude_home) / ".credentials.json"
    if dest.exists():
        return
    host = Path(os.path.expanduser("~/.claude/.credentials.json"))
    if not host.exists():
        return
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(host, dest)
    except OSError:
        logger.warning("ensure_claude_auth copy failed → %s", dest, exc_info=True)
