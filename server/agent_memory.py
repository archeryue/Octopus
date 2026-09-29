"""Per-agent native-memory provisioning (docs/plans/memory.md).

Memory is each harness's NATIVE, agent-written markdown memory, persisted and
scoped per agent under ``<agents_dir>/<agent_id>/memory/`` — one canonical
directory both harnesses point at:

- **Claude** sets ``CLAUDE_COWORK_MEMORY_PATH_OVERRIDE`` to this dir, which
  relocates only its auto-memory dir. ``CLAUDE_CONFIG_DIR`` is left at the host
  default, so Claude's session transcripts (``--resume`` data) and auth are
  untouched.
- **Codex** gets the dir's absolute path in its ``developer_instructions`` and
  reads/writes it with file tools. ``CODEX_HOME`` is unchanged.

Memory is thus fully decoupled from both harnesses' config/auth dirs. Pure path
helpers + idempotent provisioning; no global state. ``~`` is expanded at call
time so tests that monkeypatch ``$HOME`` see the override.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .config import settings


def _agents_root(user_id: str | None = None) -> Path:
    """Where agents' state lives — this account's, or the install's.

    Per account since multi-tenancy.md §6. `user_id=None` answers the legacy
    directory, which is both what a pre-accounts install has and what an
    upgraded one keeps until §9 moves it.
    """
    from .workspace import paths_for

    if user_id is None:
        return Path(os.path.expanduser(settings.agents_dir))
    return paths_for(user_id).agents


def agent_state_dir(agent_id: str, user_id: str | None = None) -> Path:
    """``<agents_root>/<agent_id>/`` — the agent's durable state root."""
    return _agents_root(user_id) / agent_id


def agent_memory_dir(agent_id: str, user_id: str | None = None) -> Path:
    """The canonical per-agent memory dir (native markdown, shared by both
    harnesses)."""
    return agent_state_dir(agent_id, user_id) / "memory"


def ensure_agent_dirs(agent_id: str, user_id: str | None = None) -> None:
    """Create the canonical ``memory/`` dir (idempotent)."""
    agent_memory_dir(agent_id, user_id).mkdir(parents=True, exist_ok=True)


def remove_agent_dir(agent_id: str, user_id: str | None = None) -> None:
    """Delete the agent's entire state tree. Called on hard agent delete;
    archiving keeps the files."""
    shutil.rmtree(agent_state_dir(agent_id, user_id), ignore_errors=True)
