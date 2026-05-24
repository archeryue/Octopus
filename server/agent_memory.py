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


def _agents_root() -> Path:
    return Path(os.path.expanduser(settings.agents_dir))


def agent_state_dir(agent_id: str) -> Path:
    """``<agents_dir>/<agent_id>/`` — the agent's durable state root."""
    return _agents_root() / agent_id


def agent_memory_dir(agent_id: str) -> Path:
    """The canonical per-agent memory dir (native markdown, shared by both
    harnesses)."""
    return agent_state_dir(agent_id) / "memory"


def ensure_agent_dirs(agent_id: str) -> None:
    """Create the canonical ``memory/`` dir (idempotent)."""
    agent_memory_dir(agent_id).mkdir(parents=True, exist_ok=True)


def remove_agent_dir(agent_id: str) -> None:
    """Delete the agent's entire state tree. Called on hard agent delete;
    archiving keeps the files."""
    shutil.rmtree(agent_state_dir(agent_id), ignore_errors=True)
