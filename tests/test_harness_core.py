"""Phase 1 harness-layer tests: the merged run engine, shared assembly, the
registry, and run_oneshot — all driven by a fake RuntimeProfile + the shared
fake CLI, so they don't need a real claude/codex binary.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from server.harness import (
    EventParser,
    Harness,
    HarnessEvent,
    HarnessOneshotError,
    OneShotContext,
    ParseOutput,
    RunConfig,
    RuntimeProfile,
    TurnContext,
    available_backends,
    get_harness,
    register,
)
from server.harness import assembly
from server.harness.registry import _REGISTRY

FAKE_CLI = Path(__file__).parent / "_fixtures" / "fake_cli.py"


# --------------------------------------------------------------------------- #
# Fake profile
# --------------------------------------------------------------------------- #


class _RawParser(EventParser):
    """Emits one event per stdout object (type from `type`, raw=obj); ends the
    stream when a `result` object arrives — mirrors the real terminal-event
    contract."""

    def parse(self, obj: dict[str, Any]) -> ParseOutput:
        ev = HarnessEvent(type=obj.get("type", "?"), raw=obj)
        return ParseOutput(events=[ev], end_of_stream=obj.get("type") == "result")


def _stream_profile(*lines: str, close_stdin: bool = False) -> RuntimeProfile:
    def build_turn_argv(ctx: TurnContext) -> tuple[list[str], dict[str, Any]]:
        return ([sys.executable, str(FAKE_CLI), "emit-lines", *lines], {"cwd": ctx.working_dir})

    return RuntimeProfile(
        backend="fake",
        binary=sys.executable,
        tools_prompt="TOOLS",
        credential_style="env_secret",
        premature_exit_recovery=False,
        close_stdin_after_start=close_stdin,
        build_turn_argv=build_turn_argv,
        new_event_parser=_RawParser,
        build_oneshot_argv=lambda ctx: ([sys.executable], {}),
        parse_oneshot_stdout=lambda s: s,
    )


def _mode_profile(mode: str, *args: str) -> RuntimeProfile:
    """A streaming profile that runs the fake CLI in an arbitrary mode."""
    def build_turn_argv(ctx: TurnContext) -> tuple[list[str], dict[str, Any]]:
        return ([sys.executable, str(FAKE_CLI), mode, *args], {"cwd": ctx.working_dir})

    return RuntimeProfile(**{**_stream_profile().__dict__, "build_turn_argv": build_turn_argv})


async def _drain(run) -> list[HarnessEvent]:
    return [ev async for ev in run.stream()]


# --------------------------------------------------------------------------- #
# Engine: streaming + lifecycle
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_engine_streams_events_and_ends(tmp_path):
    profile = _stream_profile('{"type":"hello"}', '{"type":"result"}')
    run = Harness(profile).create_run(RunConfig())
    await run.start("p", str(tmp_path))
    events = await asyncio.wait_for(_drain(run), timeout=3.0)
    await run.stop()
    assert [e.type for e in events] == ["hello", "result"]


@pytest.mark.asyncio
async def test_engine_skips_malformed_lines(tmp_path):
    def build_turn_argv(ctx):
        return ([sys.executable, str(FAKE_CLI), "bad-json"], {"cwd": ctx.working_dir})

    profile = _stream_profile()
    profile = RuntimeProfile(**{**profile.__dict__, "build_turn_argv": build_turn_argv})
    run = Harness(profile).create_run()
    await run.start("p", str(tmp_path))
    events = await asyncio.wait_for(_drain(run), timeout=3.0)
    await run.stop()
    assert [e.type for e in events] == ["good"]


@pytest.mark.asyncio
async def test_engine_close_stdin_flag(tmp_path):
    # close_stdin_after_start must not break a normal run (codex's behavior).
    profile = _stream_profile('{"type":"result"}', close_stdin=True)
    run = Harness(profile).create_run()
    await run.start("p", str(tmp_path))
    events = await asyncio.wait_for(_drain(run), timeout=3.0)
    await run.stop()
    assert [e.type for e in events] == ["result"]


@pytest.mark.asyncio
async def test_engine_starting_twice_raises(tmp_path):
    run = Harness(_stream_profile('{"type":"result"}')).create_run()
    await run.start("p", str(tmp_path))
    with pytest.raises(RuntimeError, match="already started"):
        await run.start("p", str(tmp_path))
    await run.stop()


@pytest.mark.asyncio
async def test_engine_missing_binary_raises(tmp_path):
    def build_turn_argv(ctx):
        return (["definitely-not-a-real-binary-12345"], {"cwd": ctx.working_dir})

    profile = RuntimeProfile(**{**_stream_profile().__dict__, "build_turn_argv": build_turn_argv})
    run = Harness(profile).create_run()
    with pytest.raises(FileNotFoundError, match="not found on PATH"):
        await run.start("p", str(tmp_path))


@pytest.mark.asyncio
async def test_engine_stop_idempotent(tmp_path):
    run = Harness(_stream_profile('{"type":"result"}')).create_run()
    await run.start("p", str(tmp_path))
    await asyncio.wait_for(_drain(run), timeout=3.0)
    await run.stop()
    await run.stop()  # second stop is a no-op, not an error


@pytest.mark.asyncio
async def test_engine_captures_stderr(tmp_path):
    run = Harness(_mode_profile("fail-exit")).create_run()
    await run.start("p", str(tmp_path))
    await asyncio.wait_for(_drain(run), timeout=3.0)
    await run.stop()
    assert "boom" in run.stderr_text  # fake CLI writes "boom" to stderr


@pytest.mark.asyncio
async def test_engine_stop_kills_hung_subprocess(tmp_path):
    # sleep-then ignores stdin close; stop() must escalate to SIGKILL and
    # still return within its bounded budget.
    run = Harness(_mode_profile("sleep-then", "30")).create_run()
    await run.start("p", str(tmp_path))
    await asyncio.wait_for(run.stop(), timeout=6.0)


@pytest.mark.asyncio
async def test_engine_resolves_binary_from_fallback_dir(tmp_path, monkeypatch):
    """A bare binary not on PATH but in ~/.local/bin still resolves (the
    systemd case where the service PATH strips per-user dirs)."""
    fake_bin_dir = tmp_path / ".local" / "bin"
    fake_bin_dir.mkdir(parents=True)
    fake_binary = fake_bin_dir / "my-cli-xyzzy"
    fake_binary.write_text("#!/bin/sh\nexit 0\n")
    fake_binary.chmod(0o755)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HOME", str(tmp_path))

    def build_turn_argv(ctx):
        return (["my-cli-xyzzy"], {"cwd": ctx.working_dir})

    profile = RuntimeProfile(**{**_stream_profile().__dict__, "build_turn_argv": build_turn_argv})
    run = Harness(profile).create_run()
    await run.start("p", str(tmp_path))  # resolves + spawns the trivial script
    await run.stop()


# --------------------------------------------------------------------------- #
# Shared assembly
# --------------------------------------------------------------------------- #


def test_callback_env_has_session_id_when_present():
    env = assembly.build_callback_env("sess-123")
    assert env["OCTOPUS_SESSION_ID"] == "sess-123"
    assert env["OCTOPUS_API_BASE"].startswith("http://127.0.0.1:")
    assert "OCTOPUS_AUTH_TOKEN" in env
    assert "OCTOPUS_SESSION_ID" not in assembly.build_callback_env(None)


def test_select_mcp_servers_all_by_default():
    env = assembly.build_callback_env("s")
    entries = assembly.select_mcp_servers(None, [], env)
    assert [e.key for e in entries] == ["bg", "ask", "ask_agent"]
    bg = next(e for e in entries if e.key == "bg")
    assert bg.env["OCTOPUS_SESSION_ID"] == "s"
    ask_agent_entry = next(e for e in entries if e.key == "ask_agent")
    assert ask_agent_entry.env["OCTOPUS_SESSION_ID"] == "s"
    assert ask_agent_entry.args[-1] == "server.mcp_servers.ask_agent"


def test_select_mcp_servers_subset():
    env = assembly.build_callback_env("s")
    entries = assembly.select_mcp_servers(["ask"], [], env)
    assert [e.key for e in entries] == ["ask"]


def test_select_mcp_servers_silently_drops_unknown_legacy_names():
    # Existing agents may still carry "viewer" in their stored mcp_servers list
    # from before it became a client-only flow. Assembly should treat unknown
    # names as no-ops rather than failing, so old rows keep working.
    env = assembly.build_callback_env("s")
    entries = assembly.select_mcp_servers(["viewer", "bg"], [], env)
    assert [e.key for e in entries] == ["bg"]


def test_select_mcp_servers_merges_connectors():
    class _FakeConnector:
        def mcp_key(self, inst):
            return f"github_{inst}"

        def mcp_entry(self, inst, callback_env):
            return {"command": "py", "args": ["-m", "x"], "env": {**callback_env, "OCTOPUS_INSTALLATION_ID": inst}}

    env = assembly.build_callback_env("s")
    entries = assembly.select_mcp_servers(["bg"], [(_FakeConnector(), "abc123")], env)
    assert [e.key for e in entries] == ["bg", "github_abc123"]
    assert entries[1].env["OCTOPUS_INSTALLATION_ID"] == "abc123"


def test_compose_system_prompt_orders_persona_then_tools():
    assert assembly.compose_system_prompt(None, "TOOLS", []) == "TOOLS"
    assert assembly.compose_system_prompt("PERSONA", "TOOLS", []) == "PERSONA\n\nTOOLS"


# --------------------------------------------------------------------------- #
# Registry + derived predicates
# --------------------------------------------------------------------------- #


def test_registry_register_get_and_unknown():
    # A profile under a unique backend name so we don't collide with real ones.
    profile = RuntimeProfile(**{**_stream_profile().__dict__, "backend": "fake-test-backend"})
    harness = Harness(profile)
    register(harness)
    try:
        assert get_harness("fake-test-backend") is harness
        # None resolves to the default kind (which may be unregistered in
        # Phase 1) — unknown kinds raise explicitly.
        with pytest.raises(ValueError, match="Unknown backend"):
            get_harness("no-such-backend")
        # is_available()/available_backends reflect a resolvable binary
        # (sys.executable always resolves).
        assert harness.is_available() is True
        assert "fake-test-backend" in available_backends()
    finally:
        _REGISTRY.pop("fake-test-backend", None)


def test_derived_predicates_no_codec():
    h = Harness(_stream_profile())
    assert h.can_export is False
    assert h.can_import is False
    assert h.login is None
    assert h.premature_exit_recovery is False


# --------------------------------------------------------------------------- #
# run_oneshot
# --------------------------------------------------------------------------- #


def _oneshot_profile(result_line: str, *, mode: str = "emit-lines") -> RuntimeProfile:
    import json

    def build_oneshot_argv(ctx: OneShotContext) -> tuple[list[str], dict[str, Any]]:
        return ([sys.executable, str(FAKE_CLI), mode, result_line], {})

    def parse_oneshot_stdout(s: str) -> str:
        return json.loads(s.strip().splitlines()[-1]).get("result", "")

    return RuntimeProfile(
        **{
            **_stream_profile().__dict__,
            "build_oneshot_argv": build_oneshot_argv,
            "parse_oneshot_stdout": parse_oneshot_stdout,
        }
    )


@pytest.mark.asyncio
async def test_run_oneshot_returns_text():
    harness = Harness(_oneshot_profile('{"result":"hello world"}'))
    out = await harness.run_oneshot(OneShotContext(prompt="x"))
    assert out == "hello world"


@pytest.mark.asyncio
async def test_run_oneshot_empty_raises():
    harness = Harness(_oneshot_profile('{"result":""}'))
    with pytest.raises(HarnessOneshotError) as ei:
        await harness.run_oneshot(OneShotContext(prompt="x"))
    assert ei.value.code == "empty"


@pytest.mark.asyncio
async def test_run_oneshot_not_found_raises():
    def build_oneshot_argv(ctx):
        return (["definitely-not-a-real-binary-98765"], {})

    profile = RuntimeProfile(**{**_stream_profile().__dict__, "build_oneshot_argv": build_oneshot_argv})
    with pytest.raises(HarnessOneshotError) as ei:
        await Harness(profile).run_oneshot(OneShotContext(prompt="x"))
    assert ei.value.code == "not_found"


@pytest.mark.asyncio
async def test_run_oneshot_nonzero_exit_raises():
    harness = Harness(_oneshot_profile('{"result":"x"}', mode="fail-exit"))
    with pytest.raises(HarnessOneshotError) as ei:
        await harness.run_oneshot(OneShotContext(prompt="x"))
    assert ei.value.code == "failed"
