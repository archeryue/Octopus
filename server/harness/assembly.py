"""Shared per-turn context assembly — the de-duplicated middle.

Both harnesses need the same three things computed before rendering argv:
the callback env our in-app MCP servers use to reach the host, the
selected set of MCP servers (built-ins per the agent's choice + connector
installations), and the composed system prompt (persona + the harness's
in-app-tools blurb + the connectors blurb). This module owns all three so
adding a new in-app tool (e.g. memory) is a single-point change rather
than an edit in every profile. The profile's `build_turn_argv` only
*renders* the neutral result.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .profile import McpServerEntry

# Repo root (contains the `server/` package): needed to launch each MCP
# server via `-m server.mcp_servers.<name>` and to set PYTHONPATH so the
# import resolves regardless of the child's cwd.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)

# In-app MCP servers the agent can enable (subset of these; None = all).
_BUILTIN_MODULES = {
    "viewer": "server.mcp_servers.viewer",
    "bg": "server.mcp_servers.bg",
    "ask": "server.mcp_servers.ask",
}


def repo_root() -> str:
    return _REPO_ROOT


def build_callback_env(session_id: str | None) -> dict[str, str]:
    """The env our bg/ask MCP servers use to call back into FastAPI. The
    viewer doesn't call back (it validates paths against OCTOPUS_WORKING_DIR),
    so it gets a narrower env in `select_mcp_servers`."""
    from ..config import settings as _settings  # local import: avoid cycle at load

    env: dict[str, str] = {
        "OCTOPUS_API_BASE": f"http://127.0.0.1:{_settings.port}",
        "OCTOPUS_AUTH_TOKEN": _settings.auth_token,
        "PYTHONPATH": _REPO_ROOT,
    }
    if session_id:
        env["OCTOPUS_SESSION_ID"] = session_id
    return env


def select_mcp_servers(
    mcp_servers: list[str] | None,
    connectors: list[tuple[Any, Any]],
    working_dir_abs: str,
    callback_env: dict[str, str],
) -> list[McpServerEntry]:
    """The built-in servers the agent enabled (None = all three) plus one
    entry per enabled connector installation. Order is stable: viewer, bg,
    ask, then connectors — so rendered argv is deterministic for tests."""
    builtin_specs: dict[str, dict[str, Any]] = {
        "viewer": {
            "command": sys.executable,
            "args": ["-m", _BUILTIN_MODULES["viewer"]],
            "env": {"OCTOPUS_WORKING_DIR": working_dir_abs, "PYTHONPATH": _REPO_ROOT},
        },
        "bg": {
            "command": sys.executable,
            "args": ["-m", _BUILTIN_MODULES["bg"]],
            "env": dict(callback_env),
        },
        "ask": {
            "command": sys.executable,
            "args": ["-m", _BUILTIN_MODULES["ask"]],
            "env": dict(callback_env),
        },
    }
    if mcp_servers is not None:
        selected = {k: v for k, v in builtin_specs.items() if k in mcp_servers}
    else:
        selected = builtin_specs

    entries: list[McpServerEntry] = [
        McpServerEntry(key=key, command=spec["command"], args=spec["args"], env=spec["env"])
        for key, spec in selected.items()
    ]

    # Agent-enabled connectors (connectors.md §5.6). Each contributes one
    # per-installation entry keyed `<kind>_<id6>` so two accounts of one
    # kind don't collide; the connector builds the {command,args,env} shape.
    for connector, installation in connectors:
        entry = connector.mcp_entry(installation, callback_env)
        entries.append(
            McpServerEntry(
                key=connector.mcp_key(installation),
                command=entry["command"],
                args=entry["args"],
                env=entry["env"],
            )
        )
    return entries


def render_memory_blurb(memory_dir: str) -> str:
    """Instructions for an agent's durable, cross-session markdown memory
    (docs/plans/memory.md §3). Injected only for harnesses without native
    memory (Codex); Claude relies on its own auto-injected MEMORY.md. The
    format mirrors Claude's native memory (frontmatter facts + a MEMORY.md
    index) so the two are interchangeable on a backend switch."""
    return f"""\
== Long-term memory ==

You have a durable, cross-session memory for this agent, stored as Markdown \
files at:
  {memory_dir}

At the START of a task, read `{memory_dir}/MEMORY.md` (if it exists) to recall \
what you already know about this user and their projects. When you learn a \
durable fact worth keeping for future sessions — a stable preference, a \
project constraint, how the user wants things done — record it:
  - write/update a focused file `{memory_dir}/<slug>.md` with YAML frontmatter \
(`name`, `description`, `metadata.type` = user|feedback|project|reference) \
followed by the fact in prose;
  - add/update a one-line pointer in `{memory_dir}/MEMORY.md`: \
`- [Title](<slug>.md) — short hook`.
Keep entries durable and de-duplicated: update an existing file rather than \
adding a near-duplicate, and delete a file (and its MEMORY.md line) when it \
becomes wrong. Don't record secrets, throwaway details, or this session's \
transient state. Use your normal file-read/-write tools for these files."""


def compose_system_prompt(
    persona: str | None,
    tools_prompt: str,
    connectors: list[tuple[Any, Any]],
    memory_dir: str | None = None,
    inject_memory: bool = False,
) -> str:
    """persona (if any) ahead of the harness's in-app-tools blurb, then the
    connectors blurb (if any), then the memory blurb (when the harness has no
    native memory and an agent memory dir exists). Re-sent every turn — the
    CLIs don't persist system prompts across resume."""
    from ..connectors.base import render_connectors_blurb

    out = tools_prompt
    if persona:
        out = f"{persona}\n\n{tools_prompt}"
    if connectors:
        out = f"{out}\n\n{render_connectors_blurb(connectors)}"
    if inject_memory and memory_dir:
        out = f"{out}\n\n{render_memory_blurb(memory_dir)}"
    return out
