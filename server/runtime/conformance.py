"""The provider conformance suite (runtime-providers.md §10, step 7).

Providers ship from other repositories on other schedules, so this module —
not the protocol's type hints — is the contract: a provider conforms when every
case here passes against it. It is plain asyncio with no test framework, so an
out-of-tree provider can run it under whatever runner it uses:

    from server.runtime.conformance import CASES, ConformanceTarget

    @pytest.mark.parametrize("case", CASES, ids=lambda c: c.__name__)
    async def test_conforms(case, target: ConformanceTarget):
        await case(target)

Each case raises `AssertionError` on a violation and returns None otherwise.
The children it runs are Python one-liners, run by `target.python` — the
interpreter as the runtime sees it — so the suite needs nothing inside the
runtime but a Python 3.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path

from ..workspace import WorkspaceError
from .base import ExecSpec, Execution, Runtime, StderrMode, StdinMode

# Every case finishes in well under this on a healthy provider; a hang is a
# failure, not a reason to wait.
STEP_TIMEOUT_S = 15.0


@dataclass
class ConformanceTarget:
    """One provider, set up to be tested.

    `tenant` must be an *account* (not None): confinement only applies to an
    account, and the cases write under its paths. `serve_callback(body)` must,
    while entered, answer `GET <callback_base(tenant)>/conformance` with
    `body` — it is how the callback round-trip is observed from the server's
    side, wherever the provider puts the server relative to the runtime.
    """

    runtime: Runtime
    tenant: str
    python: str
    serve_callback: Callable[[bytes], AbstractAsyncContextManager[None]]
    # The base environment a case's child runs with (PATH and the like, as
    # the runtime needs them). Cases add to it; nothing else is inherited.
    base_env: Mapping[str, str] = field(default_factory=dict)


Case = Callable[[ConformanceTarget], Awaitable[None]]


# ----------------------------------------------------------------- helpers


def _spec(
    t: ConformanceTarget,
    code: str,
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    stdin: StdinMode = "devnull",
    stderr: StderrMode = "pipe",
    process_group: bool = True,
    line_limit: int | None = None,
) -> ExecSpec:
    return ExecSpec(
        tenant=t.tenant,
        argv=[t.python, "-c", code],
        env={**t.base_env, **(env or {})},
        cwd=cwd,
        stdin=stdin,
        stderr=stderr,
        process_group=process_group,
        line_limit=line_limit,
    )


async def _run(
    t: ConformanceTarget,
    code: str,
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    stderr: StderrMode = "pipe",
) -> tuple[int, str, str]:
    ex = await t.runtime.exec(_spec(t, code, env=env, cwd=cwd, stderr=stderr))
    out, err = await asyncio.wait_for(ex.communicate(), STEP_TIMEOUT_S)
    rc = await asyncio.wait_for(ex.wait(), STEP_TIMEOUT_S)
    return rc, out.decode(), err.decode()


async def _readline(ex: Execution) -> str:
    assert ex.stdout is not None
    return (await asyncio.wait_for(ex.stdout.readline(), STEP_TIMEOUT_S)).decode()


async def _reap(ex: Execution) -> None:
    if ex.returncode is None:
        ex.signal(signal.SIGKILL, group=True)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(ex.wait(), STEP_TIMEOUT_S)


@contextlib.asynccontextmanager
async def _execution(t: ConformanceTarget, spec: ExecSpec) -> AsyncIterator[Execution]:
    ex = await t.runtime.exec(spec)
    try:
        yield ex
    finally:
        await _reap(ex)


# ------------------------------------------------------------- exec shapes


async def oneshot_sees_eof_on_stdin(t: ConformanceTarget) -> None:
    """`stdin="devnull"`: a child that reads stdin gets EOF at once (the
    `codex exec` shape) rather than blocking."""
    rc, out, _ = await _run(t, "import sys; print(len(sys.stdin.read()))")
    assert (rc, out.strip()) == (0, "0"), (rc, out)


async def exit_code_and_streams_are_separate(t: ConformanceTarget) -> None:
    rc, out, err = await _run(
        t, "import sys; print('to-out'); print('to-err', file=sys.stderr); sys.exit(3)"
    )
    assert rc == 3, rc
    assert out.strip() == "to-out", out
    assert err.strip() == "to-err", err


async def stderr_merges_into_stdout(t: ConformanceTarget) -> None:
    """`stderr="stdout"`: one ordered stream (the log-pump shape)."""
    rc, out, err = await _run(
        t,
        "import sys; print('a', flush=True); print('b', file=sys.stderr, flush=True);"
        " print('c', flush=True)",
        stderr="stdout",
    )
    assert rc == 0
    assert out.split() == ["a", "b", "c"], out
    assert err == "", err


async def cwd_is_honoured(t: ConformanceTarget) -> None:
    workspace = t.runtime.paths(t.tenant).workspace
    rc, _, _ = await _run(
        t, f"import os; os.makedirs({str(workspace)!r}, exist_ok=True)"
    )
    assert rc == 0
    rc, out, _ = await _run(t, "import os; print(os.getcwd())", cwd=str(workspace))
    assert rc == 0
    assert os.path.realpath(out.strip()) == os.path.realpath(workspace), out


async def streaming_with_stdin(t: ConformanceTarget) -> None:
    """`stdin="pipe"`: the turn engine's shape — write a line, read the
    answer, repeat, then close stdin and the child exits cleanly."""
    code = (
        "import sys\n"
        "for line in sys.stdin:\n"
        "    print('echo:' + line.strip(), flush=True)\n"
    )
    async with _execution(t, _spec(t, code, stdin="pipe")) as ex:
        assert ex.stdin is not None
        for word in ("one", "two"):
            ex.stdin.write(f"{word}\n".encode())
            await ex.stdin.drain()
            assert (await _readline(ex)).strip() == f"echo:{word}"
        ex.stdin.close()
        assert await asyncio.wait_for(ex.wait(), STEP_TIMEOUT_S) == 0


async def held_execution_resumes_after_idling(t: ConformanceTarget) -> None:
    """A held CLI (runtime-providers.md §2): left unread between turns, then
    written to again — the pipe and the process must both still be there."""
    code = (
        "import sys\n"
        "for line in sys.stdin:\n"
        "    print('turn:' + line.strip(), flush=True)\n"
    )
    async with _execution(t, _spec(t, code, stdin="pipe")) as ex:
        assert ex.stdin is not None
        ex.stdin.write(b"1\n")
        await ex.stdin.drain()
        assert (await _readline(ex)).strip() == "turn:1"
        await asyncio.sleep(1.0)
        assert ex.returncode is None, "a held execution exited while idle"
        ex.stdin.write(b"2\n")
        await ex.stdin.drain()
        assert (await _readline(ex)).strip() == "turn:2"


async def long_lines_within_line_limit(t: ConformanceTarget) -> None:
    """`line_limit` lifts the reader's per-line cap: one stream-json event
    can be megabytes (a tool result carrying a big file)."""
    size = 1024 * 1024
    spec = _spec(t, f"print('x' * {size})", line_limit=4 * 1024 * 1024)
    async with _execution(t, spec) as ex:
        line = await _readline(ex)
        assert len(line.rstrip("\n")) == size, len(line)


async def group_signal_reaches_grandchildren(t: ConformanceTarget) -> None:
    """`signal(group=True)` reaches everything the child started.

    Observed without a pid: the grandchild inherits the child's stdout, so
    stdout reaches EOF only once the grandchild is gone too. Signalling just
    the child leaves the reader blocked — the `codex login` hang."""
    code = (
        "import subprocess, sys\n"
        f"subprocess.Popen([{t.python!r}, '-c', 'import time; time.sleep(60)'])\n"
        "print('ready', flush=True)\n"
        "import time; time.sleep(60)\n"
    )
    async with _execution(t, _spec(t, code, process_group=True)) as ex:
        assert (await _readline(ex)).strip() == "ready"
        assert ex.signal(signal.SIGKILL, group=True) is True
        assert ex.stdout is not None
        rest = await asyncio.wait_for(ex.stdout.read(), STEP_TIMEOUT_S)
        assert rest == b"", rest
        rc = await asyncio.wait_for(ex.wait(), STEP_TIMEOUT_S)
        assert rc != 0, rc


async def signal_after_exit_is_harmless(t: ConformanceTarget) -> None:
    ex = await t.runtime.exec(_spec(t, "pass"))
    assert await asyncio.wait_for(ex.wait(), STEP_TIMEOUT_S) == 0
    assert ex.signal(signal.SIGTERM, group=True) is False
    assert ex.signal(signal.SIGTERM, group=False) is False


async def detached_execution_outlives_its_caller(t: ConformanceTarget) -> None:
    """A background task's process is started by one coroutine and must keep
    running after that coroutine is gone (cancelled, even)."""
    started: asyncio.Queue[Execution] = asyncio.Queue()

    async def caller() -> None:
        ex = await t.runtime.exec(
            _spec(t, "import time; print('up', flush=True); time.sleep(60)", process_group=True)
        )
        await started.put(ex)
        await asyncio.sleep(3600)

    task = asyncio.create_task(caller())
    ex = await asyncio.wait_for(started.get(), STEP_TIMEOUT_S)
    try:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert (await _readline(ex)).strip() == "up"
        await asyncio.sleep(0.5)
        assert ex.returncode is None, "the execution died with the coroutine that started it"
    finally:
        await _reap(ex)


async def environment_is_exactly_what_was_passed(t: ConformanceTarget) -> None:
    """`ExecSpec.env` is the complete environment (runtime-providers.md §4):
    a variable in the server's own environment that the spec does not carry
    must not reach the child."""
    canary = f"OCTOPUS_CONFORMANCE_CANARY_{uuid.uuid4().hex[:8].upper()}"
    os.environ[canary] = "leaked"
    try:
        rc, out, _ = await _run(
            t,
            f"import os, json; print(json.dumps([os.environ.get({canary!r}), "
            "os.environ.get('PASSED')]))",
            env={"PASSED": "yes"},
        )
    finally:
        del os.environ[canary]
    assert rc == 0
    assert json.loads(out) == [None, "yes"], out


# ------------------------------------------------------------------ network


async def exposed_port_round_trip(t: ConformanceTarget) -> None:
    """A child binds `expose().port`; this server reaches it at `dial`."""
    exposed = await t.runtime.expose(t.tenant)
    code = (
        "import socket\n"
        "s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        f"s.bind(('0.0.0.0', {exposed.port})); s.listen(1)\n"
        "print('listening', flush=True)\n"
        "c, _ = s.accept(); c.sendall(b'hello from the runtime'); c.close()\n"
    )
    async with _execution(t, _spec(t, code)) as ex:
        assert (await _readline(ex)).strip() == "listening"
        host, _, port = exposed.dial.rpartition(":")

        def _dial() -> bytes:
            with socket.create_connection((host, int(port)), timeout=STEP_TIMEOUT_S) as c:
                return c.recv(1024)

        assert await asyncio.to_thread(_dial) == b"hello from the runtime"


async def callback_round_trip(t: ConformanceTarget) -> None:
    """A child in the runtime reaches `callback_base()` and gets the
    server's answer (runtime-providers.md §7) — what every MCP tool call and
    an Application's agent API depend on."""
    body = f"server says {uuid.uuid4().hex}".encode()
    url = t.runtime.callback_base(t.tenant).rstrip("/") + "/conformance"
    async with t.serve_callback(body):
        rc, out, err = await _run(
            t,
            "import urllib.request; "
            f"print(urllib.request.urlopen({url!r}, timeout=10).read().decode())",
        )
    assert rc == 0, err
    assert out.strip() == body.decode(), out


# ---------------------------------------------------------------- the disk


async def confinement(t: ConformanceTarget) -> None:
    """`confine()` keeps its contract (runtime-providers.md §8): an absolute
    path the tenant may use, or `WorkspaceError` — for `..`, for a symlink
    that escapes, and allowing a path that does not exist yet."""
    rt, tenant = t.runtime, t.tenant
    workspace = rt.paths(tenant).workspace
    workspace.mkdir(parents=True, exist_ok=True)

    default = rt.confine(None, tenant)
    assert os.path.isabs(default) and Path(default) == workspace.resolve(), default

    fresh = rt.confine("not-yet-there/deeper", tenant)
    assert Path(fresh) == (workspace / "not-yet-there" / "deeper").resolve(), fresh

    for escape in ("../../..", "/", str(workspace / ".." / ".." / "..")):
        try:
            got = rt.confine(escape, tenant)
        except WorkspaceError:
            continue
        raise AssertionError(f"confine({escape!r}) let the tenant out: {got}")

    link = workspace / f"escape-{uuid.uuid4().hex[:6]}"
    link.symlink_to("/", target_is_directory=True)
    try:
        rt.confine(str(link), tenant)
    except WorkspaceError:
        pass
    else:
        raise AssertionError("a symlink out of the workspace was followed out of it")
    finally:
        link.unlink()


async def state_survives_release(t: ConformanceTarget) -> None:
    """A file written under `paths(tenant)` survives `release()` then
    `ensure()` (runtime-providers.md §5 — a released runtime that forgot its
    CLI logins would sign every tenant out on idle)."""
    target = t.runtime.paths(t.tenant).workspace / f"persist-{uuid.uuid4().hex[:8]}"
    payload = uuid.uuid4().hex
    rc, _, err = await _run(
        t,
        f"import os; p = {str(target)!r}; os.makedirs(os.path.dirname(p), exist_ok=True); "
        f"open(p, 'w').write({payload!r})",
    )
    assert rc == 0, err
    await t.runtime.release(t.tenant)
    await t.runtime.ensure(t.tenant)
    rc, out, err = await _run(t, f"print(open({str(target)!r}).read())")
    assert rc == 0, err
    assert out.strip() == payload, out


async def lifecycle_is_idempotent(t: ConformanceTarget) -> None:
    for _ in range(2):
        await t.runtime.ensure(t.tenant)
    rc, out, _ = await _run(t, "print('alive')")
    assert (rc, out.strip()) == (0, "alive")
    for _ in range(2):
        await t.runtime.release(t.tenant)
    await t.runtime.ensure(t.tenant)


CASES: list[Case] = [
    oneshot_sees_eof_on_stdin,
    exit_code_and_streams_are_separate,
    stderr_merges_into_stdout,
    cwd_is_honoured,
    streaming_with_stdin,
    held_execution_resumes_after_idling,
    long_lines_within_line_limit,
    group_signal_reaches_grandchildren,
    signal_after_exit_is_harmless,
    detached_execution_outlives_its_caller,
    environment_is_exactly_what_was_passed,
    exposed_port_round_trip,
    callback_round_trip,
    confinement,
    state_survives_release,
    lifecycle_is_idempotent,
]


async def run_all(target: ConformanceTarget) -> dict[str, BaseException | None]:
    """Every case against `target`; the failure per case name, None if it
    passed. Each case gets its own `ensure()` first, as the server would."""
    results: dict[str, BaseException | None] = {}
    for case in CASES:
        await target.runtime.ensure(target.tenant)
        try:
            await case(target)
        except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            results[case.__name__] = exc
        else:
            results[case.__name__] = None
    return results
