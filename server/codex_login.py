"""In-app Codex (ChatGPT) login orchestrator — `codex login --device-auth`.

The Codex analog of `oauth_login.py`. Where Claude's flow is HTTP + a pasted
code, Codex auth is *directory-backed* (codex-backend.md §7): we run
`codex login --device-auth` against a fresh per-credential `CODEX_HOME`, scrape
the verification URL + one-time code it prints, and show them in the UI. Codex
itself polls the device endpoint; when the user authorizes in their browser it
writes `auth.json` into that dir and exits 0. No secret is stored in Octopus —
the credential *is* the dir, and Codex manages the token + its refresh there.

Lifecycle: `start(label)` → spawn + scrape (returns url+code) → caller polls
`status(login_id)` until `success`/`error` → on success the route persists a
`backend_credentials` row pointing at the dir.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import uuid
from dataclasses import dataclass, field
from enum import Enum

from .config import settings
from .harness.run import _which_with_fallback, augmented_path

logger = logging.getLogger(__name__)

_LOGIN_TTL_SECONDS = 20 * 60  # device codes expire in ~15 min; pad a little
_SCRAPE_TIMEOUT_SECONDS = 30.0

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_URL_RE = re.compile(r"https?://\S+")
# One-time code like "FGAF-NRHWK" (groups of uppercase letters/digits).
_CODE_RE = re.compile(r"\b[A-Z0-9]{3,8}-[A-Z0-9]{3,8}\b")


class CodexLoginState(str, Enum):
    pending = "pending"  # url+code shown, waiting for the user to authorize
    success = "success"
    error = "error"
    cancelled = "cancelled"


@dataclass
class CodexLoginSession:
    id: str
    credential_id: str
    codex_home: str
    label: str
    verification_url: str | None = None
    user_code: str | None = None
    state: CodexLoginState = CodexLoginState.pending
    message: str | None = None
    persisted: bool = False  # set once the route saves the credential row
    _proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)
    _watchdog: asyncio.Task | None = field(default=None, repr=False)
    _scraped: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _created_at: float = field(default=0.0, repr=False)


def build_codex_login_argv() -> list[str] | None:
    """`[<codex>, "login", "--device-auth"]`, or None if codex isn't on PATH.
    A module function so tests can monkeypatch it to point at a fake CLI."""
    binary = _which_with_fallback("codex")
    if binary is None:
        return None
    return [binary, "login", "--device-auth"]


def codex_home_root() -> str:
    return os.path.expanduser(settings.codex_home_dir)


def codex_home_for(credential_id: str) -> str:
    return os.path.join(codex_home_root(), credential_id)


def _kill_process_group(proc: asyncio.subprocess.Process | None) -> None:
    """SIGKILL the whole process group. `codex login` spawns helpers that hold
    the stdout pipe open, so killing only the parent leaves our reader blocked
    until the device code expires (~15 min). We spawn it as a group leader
    (`start_new_session=True`) and kill the group so the pipe EOFs at once."""
    if proc is None or proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


class CodexLoginManager:
    """Holds in-flight Codex device-auth logins. Single instance, app-lifetime."""

    def __init__(self) -> None:
        self._sessions: dict[str, CodexLoginSession] = {}

    async def start(self, label: str) -> CodexLoginSession:
        argv = build_codex_login_argv()
        if argv is None:
            raise RuntimeError(
                "The `codex` CLI isn't installed on the server, so Codex "
                "sign-in isn't available."
            )

        credential_id = uuid.uuid4().hex[:12]
        home = codex_home_for(credential_id)
        os.makedirs(home, exist_ok=True)

        env = os.environ.copy()
        env["CODEX_HOME"] = home
        # codex is a `#!/usr/bin/env node` script; make sure the child finds
        # node even when the service PATH omits the nvm bin (else exit 127).
        env["PATH"] = augmented_path(env.get("PATH"), os.path.dirname(argv[0]))

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=home,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,  # own process group → killable as a unit
            )
        except FileNotFoundError:
            shutil.rmtree(home, ignore_errors=True)
            raise RuntimeError("Could not launch the `codex` CLI for sign-in.")

        loop = asyncio.get_running_loop()
        session = CodexLoginSession(
            id=uuid.uuid4().hex[:16],
            credential_id=credential_id,
            codex_home=home,
            label=label,
            _proc=proc,
            _created_at=loop.time(),
        )
        session._task = asyncio.create_task(self._drive(session))
        # Scrape happens asynchronously — `start` returns immediately so the
        # HTTP request can't outlive a proxy/tunnel timeout (codex blocks the
        # whole device-auth window polling for authorization). The UI polls
        # `status` for the URL+code, then for completion.
        session._watchdog = asyncio.create_task(self._scrape_watchdog(session))
        self._sessions[session.id] = session
        self._gc()

        logger.info(
            "Codex login %s: spawned (cred=%s)", session.id, credential_id
        )
        return session

    async def _scrape_watchdog(self, session: CodexLoginSession) -> None:
        """Fail the login if codex never emits a device code (e.g. it can't
        reach auth.openai.com), so it doesn't sit pending forever."""
        try:
            await asyncio.wait_for(
                asyncio.shield(session._scraped.wait()), _SCRAPE_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            if session.state == CodexLoginState.pending and not session.verification_url:
                await self._fail(
                    session,
                    "Codex didn't return a device code in time — check the "
                    "server's network access to auth.openai.com.",
                )
                await self._reap(session)

    async def _drive(self, session: CodexLoginSession) -> None:
        """Read codex's output: scrape url+code, then await exit and classify."""
        proc = session._proc
        assert proc is not None and proc.stdout is not None
        buf = ""
        try:
            async for raw in proc.stdout:
                line = _ANSI_RE.sub("", raw.decode(errors="replace"))
                buf += line
                if not session._scraped.is_set():
                    self._try_scrape(session, buf)
        except Exception:
            logger.debug("Codex login %s: stdout read error", session.id, exc_info=True)

        rc = await proc.wait()
        if session.state == CodexLoginState.cancelled:
            return
        auth_ok = os.path.exists(os.path.join(session.codex_home, "auth.json"))
        if rc == 0 and auth_ok:
            session.state = CodexLoginState.success
            logger.info("Codex login %s: success", session.id)
        else:
            await self._fail(
                session,
                "Codex sign-in did not complete."
                if rc == 0
                else f"Codex sign-in exited with status {rc}.",
            )
        session._scraped.set()  # unblock start() if it never scraped

    def _try_scrape(self, session: CodexLoginSession, buf: str) -> None:
        if session.verification_url is None:
            m = _URL_RE.search(buf)
            if m:
                session.verification_url = m.group(0).rstrip(".")
        if session.user_code is None:
            m = _CODE_RE.search(buf)
            if m:
                session.user_code = m.group(0)
        if session.verification_url and session.user_code:
            session._scraped.set()

    async def _reap(self, session: CodexLoginSession) -> None:
        """Let the drive task observe the (killed) process and reap it within
        the running loop — otherwise its transport is finalized after loop
        close (a noisy "Event loop is closed" unraisable). Bounded so a
        misbehaving child can never wedge a request."""
        if session._task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(session._task), timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            pass

    async def _fail(self, session: CodexLoginSession, message: str) -> None:
        session.state = CodexLoginState.error
        session.message = message
        _kill_process_group(session._proc)
        # Clean up so a half-written CODEX_HOME can't masquerade as valid.
        shutil.rmtree(session.codex_home, ignore_errors=True)
        logger.info("Codex login %s: error — %s", session.id, message)

    async def cancel(self, login_id: str) -> None:
        session = self._sessions.get(login_id)
        if session is None:
            return
        if session.state in (CodexLoginState.success, CodexLoginState.error):
            return
        session.state = CodexLoginState.cancelled
        session.message = "cancelled by user"
        _kill_process_group(session._proc)
        await self._reap(session)
        shutil.rmtree(session.codex_home, ignore_errors=True)
        logger.info("Codex login %s: cancelled", login_id)

    def get(self, login_id: str) -> CodexLoginSession | None:
        return self._sessions.get(login_id)

    async def shutdown(self) -> None:
        for session in list(self._sessions.values()):
            _kill_process_group(session._proc)
            if session._watchdog is not None:
                session._watchdog.cancel()
        self._sessions.clear()

    def _gc(self) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        for sid in [
            s.id
            for s in self._sessions.values()
            if now - s._created_at > _LOGIN_TTL_SECONDS
        ]:
            self._sessions.pop(sid, None)


# Singleton — wired into the FastAPI lifespan in main.py.
codex_login_manager = CodexLoginManager()
