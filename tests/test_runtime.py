"""The Runtime interface (docs/plans/runtime-providers.md).

Three things are pinned here:

* `LocalRuntime` passes the provider conformance suite — both bare and as the
  server actually holds it, wrapped in `ManagedRuntime`;
* `ManagedRuntime` makes the lifecycle decisions §3 gives the server: `ensure`
  before every `exec`, `release` only once the tenant has nothing running and
  the grace period has passed — exercised with a fake provider that records
  the calls, since LocalRuntime's are no-ops;
* the spawn sites go through the runtime, with the owning tenant — a turn, a
  one-shot, a background task, `git`, an Application backend.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import sys
from collections.abc import AsyncIterator

import pytest

from server.config import settings
from server.runtime import (
    ExecSpec,
    LocalRuntime,
    ManagedRuntime,
    get_runtime,
    load_provider,
    set_runtime,
)
from server.runtime.conformance import CASES, ConformanceTarget, run_all

TENANT = "conformance-tenant"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _local_callback_server(monkeypatch):
    """`serve_callback` for LocalRuntime: its `callback_base` is this server's
    loopback port, so stand a one-route HTTP server up on a port and point
    `settings.port` at it."""
    port = _free_port()
    monkeypatch.setattr(settings, "port", port)

    @contextlib.asynccontextmanager
    async def serve(body: bytes) -> AsyncIterator[None]:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            request = await reader.readline()
            while (await reader.readline()) not in (b"\r\n", b""):
                pass
            status = b"200 OK" if b" /conformance " in request else b"404 Not Found"
            payload = body if status.startswith(b"200") else b""
            writer.write(
                b"HTTP/1.1 " + status + b"\r\nContent-Length: "
                + str(len(payload)).encode() + b"\r\nConnection: close\r\n\r\n" + payload
            )
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", port)
        try:
            yield
        finally:
            server.close()
            await server.wait_closed()

    return serve


@pytest.fixture(params=["bare", "managed"])
def target(request, monkeypatch) -> ConformanceTarget:
    runtime = LocalRuntime() if request.param == "bare" else ManagedRuntime(LocalRuntime())
    return ConformanceTarget(
        runtime=runtime,
        tenant=TENANT,
        python=sys.executable,
        serve_callback=_local_callback_server(monkeypatch),
        base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.__name__)
async def test_local_runtime_conforms(case, target):
    await target.runtime.ensure(target.tenant)
    await case(target)


async def test_run_all_reports_every_case(monkeypatch):
    target = ConformanceTarget(
        runtime=LocalRuntime(),
        tenant=TENANT,
        python=sys.executable,
        serve_callback=_local_callback_server(monkeypatch),
        base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    results = await run_all(target)
    assert set(results) == {c.__name__ for c in CASES}
    assert {k: v for k, v in results.items() if v is not None} == {}


async def test_the_suite_catches_a_runtime_that_leaks_the_servers_environment(monkeypatch):
    """The suite is the contract only if it fails a provider that breaks it.
    One that adds the server's own environment to every child is the exact
    mistake §4 exists to prevent."""

    class Leaky(LocalRuntime):
        async def exec(self, spec: ExecSpec):
            return await super().exec(
                ExecSpec(**{**spec.__dict__, "env": {**os.environ, **spec.env}})
            )

    from server.runtime import conformance

    target = ConformanceTarget(
        runtime=Leaky(),
        tenant=TENANT,
        python=sys.executable,
        serve_callback=_local_callback_server(monkeypatch),
    )
    with pytest.raises(AssertionError):
        await conformance.environment_is_exactly_what_was_passed(target)


# --------------------------------------------------------------- lifecycle


class RecordingProvider(LocalRuntime):
    """LocalRuntime that records every lifecycle call, in order."""

    name = "recording"

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def ensure(self, tenant):
        self.calls.append(("ensure", tenant))

    async def release(self, tenant):
        self.calls.append(("release", tenant))

    async def exec(self, spec):
        self.calls.append(("exec", spec.tenant))
        return await super().exec(spec)


def _quick(tenant: str | None = "u1") -> ExecSpec:
    return ExecSpec(tenant=tenant, argv=[sys.executable, "-c", "pass"], env={})


async def test_ensure_precedes_every_exec():
    provider = RecordingProvider()
    rt = ManagedRuntime(provider, release_grace=60)
    for tenant in ("u1", "u1", "u2"):
        ex = await rt.exec(_quick(tenant))
        await ex.wait()
    assert [c for c in provider.calls if c[0] != "release"] == [
        ("ensure", "u1"), ("exec", "u1"),
        ("ensure", "u1"), ("exec", "u1"),
        ("ensure", "u2"), ("exec", "u2"),
    ]
    await rt.shutdown()


async def test_release_waits_for_the_last_execution_and_the_grace():
    provider = RecordingProvider()
    rt = ManagedRuntime(provider, release_grace=0.2)
    held = await rt.exec(
        ExecSpec(tenant="u1", argv=[sys.executable, "-c", "input()"], env={}, stdin="pipe")
    )
    quick = await rt.exec(_quick())
    await quick.wait()
    await asyncio.sleep(0.4)
    assert ("release", "u1") not in provider.calls, "released with a held process still up"
    assert rt.live("u1") == 1

    assert held.stdin is not None
    held.stdin.write(b"\n")
    held.stdin.close()
    await held.wait()
    assert rt.live("u1") == 0
    assert ("release", "u1") not in provider.calls, "released before the grace period"
    await asyncio.sleep(0.4)
    assert provider.calls.count(("release", "u1")) == 1
    await rt.shutdown()


async def test_new_work_inside_the_grace_cancels_the_release():
    provider = RecordingProvider()
    rt = ManagedRuntime(provider, release_grace=0.3)
    ex = await rt.exec(_quick())
    await ex.wait()
    await asyncio.sleep(0.15)
    ex = await rt.exec(_quick())  # a turn arrives before the runtime goes away
    await asyncio.sleep(0.25)
    assert ("release", "u1") not in provider.calls
    await ex.wait()
    await asyncio.sleep(0.45)
    assert provider.calls.count(("release", "u1")) == 1
    await rt.shutdown()


async def test_communicate_counts_as_the_end_of_an_execution():
    provider = RecordingProvider()
    rt = ManagedRuntime(provider, release_grace=0.05)
    ex = await rt.exec(_quick())
    await ex.communicate()
    assert rt.live("u1") == 0
    await asyncio.sleep(0.2)
    assert ("release", "u1") in provider.calls
    await rt.shutdown()


async def test_tenants_are_released_independently():
    provider = RecordingProvider()
    rt = ManagedRuntime(provider, release_grace=0.1)
    long = await rt.exec(
        ExecSpec(tenant="busy", argv=[sys.executable, "-c", "input()"], env={}, stdin="pipe")
    )
    short = await rt.exec(_quick("idle"))
    await short.wait()
    await asyncio.sleep(0.3)
    assert ("release", "idle") in provider.calls
    assert ("release", "busy") not in provider.calls
    long.signal(9)
    await long.wait()
    await rt.shutdown()


async def test_shutdown_drops_pending_releases():
    provider = RecordingProvider()
    rt = ManagedRuntime(provider, release_grace=0.2)
    ex = await rt.exec(_quick())
    await ex.wait()
    await rt.shutdown()
    await asyncio.sleep(0.4)
    assert ("release", "u1") not in provider.calls


async def test_a_failing_release_is_logged_not_raised(caplog):
    class Broken(RecordingProvider):
        async def release(self, tenant):
            raise RuntimeError("provider is down")

    rt = ManagedRuntime(Broken(), release_grace=0.05)
    ex = await rt.exec(_quick())
    await ex.wait()
    await asyncio.sleep(0.2)
    assert "release(u1) failed" in caplog.text
    await rt.shutdown()


async def test_managed_execution_passes_the_local_pid_through():
    rt = ManagedRuntime(LocalRuntime())
    ex = await rt.exec(_quick())
    assert isinstance(ex.pid, int) and ex.pid > 0
    await ex.wait()
    await rt.shutdown()


# ---------------------------------------------------------------- registry


def test_the_default_provider_is_local():
    assert settings.runtime_provider == "local"
    assert isinstance(get_runtime().provider, LocalRuntime)


def test_load_provider_imports_a_named_class():
    rt = load_provider("tests.test_runtime:RecordingProvider")
    assert isinstance(rt, RecordingProvider)


@pytest.mark.parametrize("bad", ["", "nope", "module.only", ":Class", "module:"])
def test_load_provider_refuses_a_malformed_name(bad):
    with pytest.raises(ValueError):
        load_provider(bad)


def test_set_runtime_wraps_a_bare_provider_and_resets():
    provider = RecordingProvider()
    previous = set_runtime(provider)
    try:
        current = get_runtime()
        assert isinstance(current, ManagedRuntime) and current.provider is provider
    finally:
        set_runtime(previous)
    set_runtime(None)
    assert isinstance(get_runtime().provider, LocalRuntime)


# ------------------------------------------------- the spawn sites use it


@pytest.fixture
def recording():
    provider = RecordingProvider()
    previous = set_runtime(ManagedRuntime(provider, release_grace=3600))
    try:
        yield provider
    finally:
        set_runtime(previous)


def _execs(provider: RecordingProvider) -> list[object]:
    return [tenant for kind, tenant in provider.calls if kind == "exec"]


async def test_a_turn_runs_in_its_owners_runtime(recording, tmp_path):
    from server.harness.profile import StdinMode
    from server.harness.run import HarnessRun, RunConfig

    from .test_harness_core import _stream_profile

    profile = _stream_profile(stdin_mode=StdinMode.CLOSE_AFTER_SPAWN)
    run = HarnessRun(profile, RunConfig(user_id="owner-1"))
    await run.start("hi", str(tmp_path))
    await run.stop()
    assert _execs(recording) == ["owner-1"]


async def test_a_oneshot_runs_in_its_callers_runtime(recording):
    from server.harness import Harness
    from server.harness.events import HarnessOneshotError
    from server.harness.profile import OneShotContext, RuntimeProfile

    from .test_harness_core import _stream_profile

    profile = RuntimeProfile(
        **{
            **_stream_profile().__dict__,
            "build_oneshot_argv": lambda ctx: ([sys.executable, "-c", "pass"], {}),
        }
    )
    with pytest.raises(HarnessOneshotError):  # empty output — the spawn is what's pinned
        await Harness(profile).run_oneshot(OneShotContext(prompt="x", user_id="owner-2"))
    assert _execs(recording) == ["owner-2"]


async def test_a_background_task_runs_in_its_sessions_runtime(recording, tmp_path):
    from server.bg_tasks import BgTaskManager, BgTaskRecord
    from server.database import Database

    from .test_bg_tasks import _make_session_row

    db = Database(":memory:")
    await db.initialize()
    done = asyncio.Event()

    async def deliver(rec: BgTaskRecord) -> None:
        done.set()

    async def broadcast(msg: dict) -> None:
        pass

    manager = BgTaskManager()
    manager.bind(db=db, deliver_cb=deliver, broadcast_cb=broadcast)
    try:
        await _make_session_row(db, "s1", str(tmp_path))
        await manager.start_task(
            session_id="s1", command="true", working_dir=str(tmp_path), tenant="owner-3"
        )
        await asyncio.wait_for(done.wait(), 10)
        assert _execs(recording) == ["owner-3"]
    finally:
        await manager.shutdown()
        await db.close()


async def test_git_runs_in_the_tenants_runtime(recording, tmp_path):
    from server import fork_helpers

    await fork_helpers.capture_git_anchor(str(tmp_path), tenant="owner-4")
    assert _execs(recording) == ["owner-4"]


async def test_an_application_backend_runs_in_its_owners_runtime(recording, tmp_path):
    from server.app_backends import RUNNING, BackendSupervisor

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    start = app_dir / "start.sh"
    start.write_text(
        '#!/usr/bin/env bash\nexec python3 -m http.server "$PORT" --bind 127.0.0.1\n'
    )
    start.chmod(0o755)
    supervisor = BackendSupervisor()
    try:
        st = await supervisor.ensure_running("app-1", str(app_dir), tenant="owner-5")
        assert st.state == RUNNING, st.error
        assert st.dial == f"127.0.0.1:{st.port}"
        assert _execs(recording) == ["owner-5"]
    finally:
        await supervisor.stop_all()


def test_the_agent_api_url_comes_from_the_runtime(monkeypatch):
    from server.app_backends import script_env

    class Elsewhere(LocalRuntime):
        def callback_base(self, tenant):
            return f"http://server.internal:9000/{tenant}"

    previous = set_runtime(Elsewhere())
    try:
        env = script_env("app-a", "/tmp/app-a", tenant="t9")
    finally:
        set_runtime(previous)
    assert env["OCTOPUS_AGENT_API"] == "http://server.internal:9000/t9/apps/app-a/agent"


def test_mcp_urls_come_from_the_runtime():
    from server.harness import assembly

    class Elsewhere(LocalRuntime):
        def callback_base(self, tenant):
            return "http://server.internal:9000"

    previous = set_runtime(Elsewhere())
    try:
        entries = assembly.select_mcp_servers(["bg"], [], {}, session_id="s-1", user_id="t1")
        env = assembly.build_callback_env("s-1", "t1")
    finally:
        set_runtime(previous)
    assert entries[0].url.startswith("http://server.internal:9000/")
    assert env["OCTOPUS_API_BASE"] == "http://server.internal:9000"
