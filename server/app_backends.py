"""Supervised backend processes for Applications (application-backends.md).

An Application declares a backend by shipping an executable `start.sh` at its
root. There is no manifest: Octopus allocates a port, sets a handful of
environment variables, runs the script, waits for the port to accept, and
proxies `/apps/{id}/api/…` to it. It never learns what Python or Node is —
`install.sh` does that, and the agent that wrote the app wrote it.

What lives here is the lifecycle: install, lazy start, readiness, idle stop,
restart on rebuild, crash reporting, and the logs that make a dead backend
debuggable. Routing lives in `routers/applications.py`; the directory layout
and the reasoning behind it are in the plan doc §3.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .applications import (
    app_scope_token,
    backend_script,
    data_dir_for,
    runtime_dir_for,
)
from .config import settings

logger = logging.getLogger(__name__)

# Backend states, the second axis alongside the build status. An app can be
# perfectly built with a crashed backend, or mid-rebuild while the old backend
# still serves — one enum covering both would be wrong in one of those cases
# (plan §9).
ABSENT = "absent"
INSTALLING = "installing"
STOPPED = "stopped"
STARTING = "starting"
RUNNING = "running"
FAILED = "failed"

# How long a backend may take to bind its port before we call it broken. A
# daemonizing `start.sh` looks exactly like this from the outside, which is the
# most likely way for an agent to get the contract wrong.
READY_TIMEOUT_S = 30.0
# Dependency installs pull from the network; give them room, but not forever.
INSTALL_TIMEOUT_S = 600.0
# Idle backends are stopped. The next request starts one again — same bargain
# as the held-CLI reaper, and the same reason: a workspace with a dozen apps
# shouldn't be a workspace with a dozen idle servers.
IDLE_TIMEOUT_S = 15 * 60.0
REAP_INTERVAL_S = 60.0
# Hard cap on concurrently running backends, least-recently-used evicted first.
MAX_RUNNING = 4
# Log tail kept in memory for the UI; the full log is on disk.
LOG_TAIL_LINES = 200
# A crashed backend is retried, but not in a tight loop.
RETRY_BACKOFF_S = 30.0


def _free_port() -> int:
    """Ask the OS for an unused port, then hand the number to the child.

    There is an unavoidable gap between closing this socket and the child
    binding it. Binding in the parent and passing the fd would close that gap
    but would force every backend to accept an inherited descriptor — a much
    worse contract for a shell script. A collision here is rare, and shows up
    as a start failure with the child's own "address in use" in the log.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _port_accepts(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        return s.connect_ex(("127.0.0.1", port)) == 0


def script_env(app_id: str, app_dir: str, *, port: int | None = None) -> dict[str, str]:
    """The environment a backend script runs with.

    Deliberately NOT `os.environ.copy()`. The server's environment holds the
    Octopus auth token, credential material and tunnel config; a backend has no
    business seeing any of it, and inheriting it wholesale is invisible until
    it isn't (plan §4).
    """
    env = {
        "APP_ID": app_id,
        "APP_DIR": app_dir,
        "APP_DATA_DIR": data_dir_for(app_dir),
        "APP_RUNTIME_DIR": runtime_dir_for(app_dir),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        # How a backend talks to the Octopus agents (app-agent-access.md §4).
        # The token is scoped to this one application, so handing it to app
        # code doesn't hand over Octopus; the URL is the loopback origin, not
        # the tunnel, because the backend is on this machine.
        "OCTOPUS_AGENT_API": f"http://127.0.0.1:{settings.port}/apps/{app_id}/agent",
        "OCTOPUS_APP_TOKEN": app_scope_token(app_id),
    }
    if port is not None:
        env["PORT"] = str(port)
    return env


@dataclass
class BackendState:
    """Live state for one application's backend."""

    app_id: str
    app_dir: str
    state: str = ABSENT
    port: int | None = None
    pid: int | None = None
    error: str | None = None
    started_at: float | None = None
    last_used: float = field(default_factory=time.monotonic)
    # Set once install has succeeded for the current code; cleared by a rebuild.
    installed: bool = False
    failed_at: float | None = None
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    log: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_TAIL_LINES), repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _readers: list[asyncio.Task[None]] = field(default_factory=list, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "port": self.port,
            "error": self.error,
            "uptime_s": (
                round(time.monotonic() - self.started_at, 1)
                if self.started_at and self.state == RUNNING
                else None
            ),
            "log_tail": list(self.log),
        }


class BackendSupervisor:
    """App-lifetime singleton owning every application's backend process."""

    def __init__(self) -> None:
        self._backends: dict[str, BackendState] = {}
        self._reaper: asyncio.Task[None] | None = None

    # ---------------------------------------------------------------- state

    def get(self, app_id: str) -> BackendState | None:
        return self._backends.get(app_id)

    def describe(self, app_id: str, app_dir: str) -> dict[str, Any]:
        """Public backend status for the API, without starting anything."""
        st = self._backends.get(app_id)
        if st is not None:
            return st.public()
        absent = BackendState(app_id=app_id, app_dir=app_dir)
        absent.state = STOPPED if backend_script(app_dir, "start.sh") else ABSENT
        return absent.public()

    def _state_for(self, app_id: str, app_dir: str) -> BackendState:
        st = self._backends.get(app_id)
        if st is None or st.app_dir != app_dir:
            st = BackendState(app_id=app_id, app_dir=app_dir)
            self._backends[app_id] = st
        return st

    # -------------------------------------------------------------- logging

    def _log(self, st: BackendState, line: str) -> None:
        st.log.append(line)
        path = os.path.join(runtime_dir_for(st.app_dir), "logs")
        try:
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "backend.log"), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass  # a log we can't write must never break the app

    async def _pump(self, st: BackendState, stream: asyncio.StreamReader, tag: str) -> None:
        try:
            async for raw in stream:
                self._log(st, f"[{tag}] {raw.decode(errors='replace').rstrip()}")
        except asyncio.CancelledError:
            pass  # the backend is being stopped; the pump goes with it
        except Exception:
            logger.debug("%s pump for %s failed", tag, st.app_id, exc_info=True)

    # -------------------------------------------------------------- install

    async def install(self, app_id: str, app_dir: str) -> bool:
        """Run `install.sh` if the app ships one. True if the app is ready to start.

        A failed install is a state, not an exception: the app reports
        `failed` with the tail of the output, because "it doesn't work" with no
        log is the exact failure this feature exists to avoid.
        """
        st = self._state_for(app_id, app_dir)
        script = backend_script(app_dir, "install.sh")
        if script is None:
            st.installed = True
            return True

        st.state = INSTALLING
        st.error = None
        self._log(st, "--- install.sh ---")
        os.makedirs(runtime_dir_for(app_dir), exist_ok=True)
        os.makedirs(data_dir_for(app_dir), exist_ok=True)
        try:
            proc = await asyncio.create_subprocess_exec(
                script,
                cwd=app_dir,
                env=script_env(app_id, app_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            st.state = FAILED
            st.error = f"install.sh could not be run: {exc}"
            self._log(st, st.error)
            return False

        assert proc.stdout is not None  # spawned with stdout=PIPE
        pump = asyncio.create_task(self._pump(st, proc.stdout, "install"))
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=INSTALL_TIMEOUT_S)
        except TimeoutError:
            _kill_group(proc)
            st.state = FAILED
            st.error = f"install.sh timed out after {INSTALL_TIMEOUT_S:.0f}s"
            self._log(st, st.error)
            return False
        finally:
            pump.cancel()
            with contextlib.suppress(Exception):
                await pump

        if code != 0:
            st.state = FAILED
            st.error = f"install.sh exited {code}"
            self._log(st, st.error)
            return False
        st.installed = True
        st.state = STOPPED
        self._log(st, "install.sh ok")
        return True

    # ---------------------------------------------------------------- start

    async def ensure_running(self, app_id: str, app_dir: str) -> BackendState:
        """Start the backend if it isn't up. Returns its state either way.

        Lazy by design: called from the proxy, so a backend exists only once
        something actually asks it for something.
        """
        st = self._state_for(app_id, app_dir)
        st.last_used = time.monotonic()

        if backend_script(app_dir, "start.sh") is None:
            st.state = ABSENT
            return st

        async with st.lock:
            if st.state == RUNNING and st.process and st.process.returncode is None:
                return st
            if (
                st.state == FAILED
                and st.failed_at
                and time.monotonic() - st.failed_at < RETRY_BACKOFF_S
            ):
                return st  # don't retry a crash in a tight loop

            if not st.installed and not await self.install(app_id, app_dir):
                st.failed_at = time.monotonic()
                return st

            await self._evict_if_over_cap(keep=app_id)
            await self._start_locked(st)
            return st

    async def _start_locked(self, st: BackendState) -> None:
        script = backend_script(st.app_dir, "start.sh")
        if script is None:
            st.state = ABSENT
            return
        port = _free_port()
        st.state = STARTING
        st.error = None
        st.port = port
        self._log(st, f"--- start.sh (port {port}) ---")
        os.makedirs(data_dir_for(st.app_dir), exist_ok=True)
        try:
            proc = await asyncio.create_subprocess_exec(
                script,
                cwd=st.app_dir,
                env=script_env(st.app_id, st.app_dir, port=port),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                # Its own group: a backend that spawns children (a git process,
                # a worker) must not outlive its app, and only a group signal
                # reaches them.
                start_new_session=True,
            )
        except OSError as exc:
            st.state = FAILED
            st.error = f"start.sh could not be run: {exc}"
            st.failed_at = time.monotonic()
            self._log(st, st.error)
            return

        st.process = proc
        st.pid = proc.pid
        assert proc.stdout is not None  # spawned with stdout=PIPE
        st._readers = [asyncio.create_task(self._pump(st, proc.stdout, "app"))]

        deadline = time.monotonic() + READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if proc.returncode is not None:
                st.state = FAILED
                st.error = f"start.sh exited {proc.returncode} before binding port {port}"
                st.failed_at = time.monotonic()
                self._log(st, st.error)
                return
            if await asyncio.to_thread(_port_accepts, port):
                st.state = RUNNING
                st.started_at = time.monotonic()
                self._log(st, f"backend ready on port {port}")
                return
            await asyncio.sleep(0.2)

        st.state = FAILED
        st.error = (
            f"backend did not accept a connection on port {port} within "
            f"{READY_TIMEOUT_S:.0f}s — start.sh must run the server in the "
            f"FOREGROUND (use exec), not background it"
        )
        st.failed_at = time.monotonic()
        self._log(st, st.error)
        await self._stop_state(st)

    # ----------------------------------------------------------------- stop

    async def stop(self, app_id: str) -> None:
        st = self._backends.get(app_id)
        if st is None:
            return
        async with st.lock:
            await self._stop_state(st)

    async def _stop_state(self, st: BackendState) -> None:
        proc = st.process
        st.process = None
        st.pid = None
        st.port = None
        st.started_at = None
        for task in st._readers:
            task.cancel()
            with contextlib.suppress(Exception):
                await task
        st._readers = []
        if st.state not in (FAILED, ABSENT):
            st.state = STOPPED
        if proc is None or proc.returncode is not None:
            return
        _kill_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            _kill_group(proc, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5.0)

    async def on_rebuild(self, app_id: str, app_dir: str) -> None:
        """A build turn rewrote the code; the running process is still executing
        the old one, and its dependencies may have changed."""
        st = self._backends.get(app_id)
        if st is None:
            return
        await self.stop(app_id)
        st.installed = False
        st.state = STOPPED if backend_script(app_dir, "start.sh") else ABSENT
        st.error = None
        st.failed_at = None

    async def stop_all(self) -> int:
        stopped = 0
        for app_id in list(self._backends):
            st = self._backends[app_id]
            if st.process is not None:
                stopped += 1
            await self.stop(app_id)
        return stopped

    # ---------------------------------------------------------------- reaper

    async def _evict_if_over_cap(self, *, keep: str) -> None:
        running = [
            s
            for s in self._backends.values()
            if s.state == RUNNING and s.app_id != keep
        ]
        if len(running) < MAX_RUNNING:
            return
        running.sort(key=lambda s: s.last_used)
        for st in running[: len(running) - MAX_RUNNING + 1]:
            self._log(st, "stopped to stay under the running-backend cap")
            await self._stop_state(st)

    async def reap_idle(self) -> int:
        now = time.monotonic()
        stopped = 0
        for st in list(self._backends.values()):
            if st.state != RUNNING:
                continue
            if now - st.last_used < IDLE_TIMEOUT_S:
                continue
            async with st.lock:
                self._log(st, "stopped after idle timeout")
                await self._stop_state(st)
            stopped += 1
        return stopped

    async def _reap_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(REAP_INTERVAL_S)
                await self.reap_idle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("backend reaper iteration failed")

    def start_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(
                self._reap_loop(), name="app-backend-reaper"
            )

    async def shutdown(self) -> None:
        task = self._reaper
        self._reaper = None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(Exception):
                await task
        await self.stop_all()


def _kill_group(proc: asyncio.subprocess.Process, sig: int = signal.SIGKILL) -> None:
    """Signal the child's whole process group.

    `start_new_session=True` made the child a group leader, so this reaches
    anything it spawned. Signalling just the pid leaves a `git clone` or a
    worker running with no parent.
    """
    with contextlib.suppress(Exception):
        os.killpg(os.getpgid(proc.pid), sig)


backend_supervisor = BackendSupervisor()
