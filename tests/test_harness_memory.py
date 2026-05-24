"""Per-agent native memory → harness wiring (docs/plans/memory.md §3-4).

Asserts the *rendered* turn for each harness: Claude gets a per-agent
CLAUDE_CONFIG_DIR and NO prompt blurb (it has native memory); Codex gets the
memory blurb in developer_instructions and never enables features.memories.
Pure argv/env inspection — no subprocess, no FS side effects (those live in
prepare_workspace, which build_argv does not call).
"""

from __future__ import annotations

import pytest

from server.harness import RunConfig, get_harness
from server.harness.assembly import compose_system_prompt, render_memory_blurb


def _arg_after(argv, flag):
    return argv[argv.index(flag) + 1]


# --- profile flags ---------------------------------------------------------


def test_profile_memory_flags():
    codex = get_harness("codex").profile
    claude = get_harness("claude-code").profile
    assert codex.injects_memory_prompt is True
    assert codex.prepare_workspace is None          # memory decoupled from CODEX_HOME
    assert claude.injects_memory_prompt is False     # native memory instead
    assert claude.prepare_workspace is not None       # symlink + auth seed


# --- assembly --------------------------------------------------------------


def test_render_memory_blurb_names_dir_and_format():
    blurb = render_memory_blurb("/agents/a1/memory")
    assert "/agents/a1/memory" in blurb
    assert "MEMORY.md" in blurb
    assert "frontmatter" in blurb


def test_compose_system_prompt_memory_gating():
    on = compose_system_prompt(None, "TOOLS", [], memory_dir="/x/mem", inject_memory=True)
    assert "== Long-term memory ==" in on and "/x/mem" in on
    off = compose_system_prompt(None, "TOOLS", [], memory_dir="/x/mem", inject_memory=False)
    assert "== Long-term memory ==" not in off
    no_dir = compose_system_prompt(None, "TOOLS", [], memory_dir=None, inject_memory=True)
    assert "== Long-term memory ==" not in no_dir


# --- Claude ----------------------------------------------------------------


def test_claude_sets_per_agent_config_dir(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    run = get_harness("claude-code").create_run(
        RunConfig(session_id="s1", memory_dir="/ag/a/memory", agent_config_dir="/ag/a/claude-home")
    )
    argv, kwargs = run.build_argv("hi", "/tmp", None)
    assert kwargs["env"]["CLAUDE_CONFIG_DIR"] == "/ag/a/claude-home"


def test_claude_no_config_dir_without_agent(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    run = get_harness("claude-code").create_run(RunConfig(session_id="s1"))
    argv, kwargs = run.build_argv("hi", "/tmp", None)
    assert "CLAUDE_CONFIG_DIR" not in kwargs["env"]


def test_claude_omits_memory_blurb_even_with_memory_dir():
    run = get_harness("claude-code").create_run(
        RunConfig(session_id="s1", memory_dir="/ag/a/memory", agent_config_dir="/ag/a/claude-home")
    )
    argv, _ = run.build_argv("hi", "/tmp", None)
    assert "== Long-term memory ==" not in _arg_after(argv, "--append-system-prompt")


# --- Codex -----------------------------------------------------------------


def test_codex_injects_memory_blurb_and_no_native_feature():
    run = get_harness("codex").create_run(
        RunConfig(session_id="s1", memory_dir="/ag/a/memory")
    )
    argv, _ = run.build_argv("hi", "/tmp", None)
    joined = " ".join(argv)
    assert "== Long-term memory ==" in joined
    assert "/ag/a/memory" in joined
    # We do NOT use Codex's native memory pipeline.
    assert "features.memories" not in joined


def test_codex_no_blurb_without_memory_dir():
    run = get_harness("codex").create_run(RunConfig(session_id="s1"))
    argv, _ = run.build_argv("hi", "/tmp", None)
    assert "== Long-term memory ==" not in " ".join(argv)
