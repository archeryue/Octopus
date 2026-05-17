"""In-app OAuth login for Claude Code subscriptions.

Drives `claude setup-token` from a child subprocess with a PTY so its Ink
TUI renders normally. Extracts the authorize URL from the PTY output,
hands it to the WebUI, then accepts a code from the user, pipes it back
to the subprocess, and captures the resulting long-lived API key from the
subprocess output.

Design notes live in `docs/cli-protocol-notes.md` under "OAuth / login
surface". The token shape is a normal `sk-ant-…` API key, so the rest of
the credential system (encrypted storage, ANTHROPIC_API_KEY injection
on session spawn) is reused as-is — this module only handles the
acquisition step.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pty
import re
import shutil
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# Regex to pull a Claude OAuth authorize URL out of the Ink-rendered output.
# We accept both the consumer (claude.ai) and console (anthropic.com)
# variants the CLI may print.
_URL_RE = re.compile(
    r"https?://(?:claude\.ai|console\.anthropic\.com)/oauth/authorize\S*"
)

# Regex to find the long-lived token the CLI prints on success.
# Format observed in the bundled CLI: "Your token is sk-ant-…" or just the
# bare token on its own line. We grep for the sk-ant- prefix.
_TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")

# How long to wait for the authorize URL to appear after spawn.
_URL_TIMEOUT_S = 20.0

# How long to wait for the token after submitting the code.
_TOKEN_TIMEOUT_S = 60.0


class LoginState(str, Enum):
    starting = "starting"        # subprocess spawned, waiting for URL
    awaiting_code = "awaiting_code"  # URL surfaced, waiting for user to paste code
    finalizing = "finalizing"    # code submitted, waiting for token
    success = "success"          # token captured
    error = "error"              # something blew up; see `message`
    cancelled = "cancelled"      # user aborted


@dataclass
class LoginSession:
    """One in-flight OAuth attempt.

    `id` is the public handle the WebUI references; it has no relationship
    to the credential row yet (the row is only persisted after the token
    is captured). `binary` is the resolved CLI path for diagnostics.
    """

    id: str
    binary: str
    state: LoginState = LoginState.starting
    url: str | None = None
    token: str | None = None
    message: str | None = None  # error detail / status message
    _process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _master_fd: int | None = field(default=None, repr=False)
    _output: list[str] = field(default_factory=list, repr=False)
    _reader_task: asyncio.Task | None = field(default=None, repr=False)
    _url_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _token_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _done_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


class OAuthLoginManager:
    """Owns all active login subprocesses, keyed by login id.

    Single-instance, lives on the FastAPI app for the process lifetime.
    Cleans up at shutdown so we don't leak `claude` processes.
    """

    binary_name = "claude"

    def __init__(self) -> None:
        self._sessions: dict[str, LoginSession] = {}

    # ------------------------------------------------------------------ public API

    async def start(self) -> LoginSession:
        """Spawn a fresh `claude setup-token` subprocess in a PTY and wait
        for the authorize URL to appear.

        Returns the LoginSession with `state == awaiting_code` and `url`
        populated. Raises on spawn failure or URL timeout.
        """
        binary = shutil.which(self.binary_name)
        if binary is None:
            # Same fallback search the SubprocessJsonlBackend uses — the
            # service unit may have stripped ~/.local/bin from PATH.
            from .backends.subprocess_jsonl import _which_with_fallback
            binary = _which_with_fallback(self.binary_name)
        if binary is None:
            raise FileNotFoundError(
                f"{self.binary_name} CLI not found on PATH — install Claude Code first"
            )

        login_id = uuid.uuid4().hex[:16]
        session = LoginSession(id=login_id, binary=binary)
        self._sessions[login_id] = session

        # PTY so the CLI's Ink TUI thinks it has a real terminal.
        master_fd, slave_fd = pty.openpty()
        session._master_fd = master_fd

        logger.info(
            "OAuth login %s: spawning %s setup-token (pty master_fd=%d)",
            login_id,
            binary,
            master_fd,
        )
        try:
            session._process = await asyncio.create_subprocess_exec(
                binary,
                "setup-token",
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                # Detach from our process group so signals don't propagate
                # weirdly through the PTY.
                start_new_session=True,
            )
        except Exception as e:
            os.close(master_fd)
            os.close(slave_fd)
            session.state = LoginState.error
            session.message = f"spawn failed: {e}"
            self._sessions.pop(login_id, None)
            logger.exception("OAuth login %s: spawn failed", login_id)
            raise
        finally:
            # The subprocess inherits the slave fd; we must not keep our
            # own reference or the PTY won't deliver EOF when it exits.
            os.close(slave_fd)

        session._reader_task = asyncio.create_task(
            self._read_pty(session), name=f"oauth-reader-{login_id}"
        )

        # Wait for the URL or for the subprocess to die early. The reader
        # sets _url_event both on successful URL match AND on EOF (so we
        # don't camp on a dead subprocess) — check session.url to tell them
        # apart.
        try:
            await asyncio.wait_for(session._url_event.wait(), timeout=_URL_TIMEOUT_S)
        except asyncio.TimeoutError:
            captured = _strip_ansi("".join(session._output))
            await self._cleanup(session)
            session.state = LoginState.error
            session.message = (
                f"Timed out after {_URL_TIMEOUT_S:.0f}s waiting for authorize URL "
                f"from `claude setup-token`. Last output: {captured[-600:]!r}"
            )
            logger.warning(
                "OAuth login %s: URL timeout. Full captured output (ansi-stripped):\n%s",
                login_id,
                captured,
            )
            raise RuntimeError(session.message)

        if session.url is None:
            captured = _strip_ansi("".join(session._output))
            await self._cleanup(session)
            session.state = LoginState.error
            session.message = (
                "`claude setup-token` exited before printing an authorize URL. "
                f"Output: {captured[-600:]!r}"
            )
            logger.warning(
                "OAuth login %s: subprocess exited without URL. Output (ansi-stripped):\n%s",
                login_id,
                captured,
            )
            raise RuntimeError(session.message)

        logger.info(
            "OAuth login %s: got URL, awaiting user code (url=%s)",
            login_id,
            session.url,
        )
        session.state = LoginState.awaiting_code
        return session

    async def submit_code(self, login_id: str, code: str) -> LoginSession:
        """Pipe the user's pasted code back into the running CLI and wait
        for the long-lived token to be printed.

        Returns the LoginSession with `state == success` and `token`
        populated. Raises if the subprocess errored or didn't yield a token.
        """
        session = self._sessions.get(login_id)
        if session is None:
            raise KeyError(f"unknown login id: {login_id}")
        if session.state != LoginState.awaiting_code:
            raise RuntimeError(
                f"login {login_id} is in state {session.state}, "
                f"cannot accept code"
            )
        if session._master_fd is None:
            raise RuntimeError("login session has no PTY (already cleaned up?)")

        session.state = LoginState.finalizing
        logger.info(
            "OAuth login %s: submitting code (%d chars)", login_id, len(code.strip())
        )
        # The CLI's prompt expects the code + Enter. Write both.
        os.write(session._master_fd, (code.strip() + "\n").encode())

        try:
            await asyncio.wait_for(session._token_event.wait(), timeout=_TOKEN_TIMEOUT_S)
        except asyncio.TimeoutError:
            captured = _strip_ansi("".join(session._output))
            await self._cleanup(session)
            session.state = LoginState.error
            session.message = (
                f"Timed out after {_TOKEN_TIMEOUT_S:.0f}s waiting for token. "
                f"Last output: {captured[-600:]!r}"
            )
            logger.warning(
                "OAuth login %s: token timeout. Full captured output:\n%s",
                login_id,
                captured,
            )
            raise RuntimeError(session.message)

        if session.token is None:
            captured = _strip_ansi("".join(session._output))
            await self._cleanup(session)
            session.state = LoginState.error
            session.message = (
                "subprocess exited without producing a token. "
                f"Output: {captured[-600:]!r}"
            )
            logger.warning(
                "OAuth login %s: subprocess exited without token. Output:\n%s",
                login_id,
                captured,
            )
            raise RuntimeError(session.message)

        logger.info("OAuth login %s: token captured, success", login_id)
        session.state = LoginState.success
        await self._cleanup(session)
        return session

    async def cancel(self, login_id: str) -> None:
        """Abort an in-flight login. Idempotent."""
        session = self._sessions.get(login_id)
        if session is None:
            return
        if session.state in (LoginState.success, LoginState.error, LoginState.cancelled):
            return
        session.state = LoginState.cancelled
        session.message = "cancelled by user"
        await self._cleanup(session)

    def get(self, login_id: str) -> LoginSession | None:
        return self._sessions.get(login_id)

    async def shutdown(self) -> None:
        """Tear down all live login subprocesses (called at app shutdown)."""
        for sid in list(self._sessions.keys()):
            try:
                await self.cancel(sid)
            except Exception:
                logger.exception("cancel during shutdown failed for %s", sid)

    # ------------------------------------------------------------------ internals

    async def _read_pty(self, session: LoginSession) -> None:
        """Stream the PTY master fd into session._output, signaling the
        url/token events when patterns appear."""
        assert session._master_fd is not None
        loop = asyncio.get_running_loop()

        try:
            while True:
                try:
                    chunk = await loop.run_in_executor(
                        None, _read_chunk, session._master_fd
                    )
                except OSError:
                    # PTY closed (subprocess exited)
                    break
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                session._output.append(text)
                self._inspect(session, text)
        finally:
            # Subprocess wait — non-blocking even if it's already gone.
            if session._process is not None:
                try:
                    await asyncio.wait_for(session._process.wait(), timeout=1.0)
                except (asyncio.TimeoutError, ProcessLookupError):
                    pass
            session._done_event.set()
            # Unblock any waiter that was expecting URL/token; the public
            # methods will see the absent fields and raise.
            session._url_event.set()
            session._token_event.set()

    def _inspect(self, session: LoginSession, chunk: str) -> None:
        """Scan freshly-read PTY text for the URL and the token."""
        if session.url is None:
            m = _URL_RE.search(chunk)
            if m is None:
                # The Ink renderer often inserts ANSI escapes mid-URL; do a
                # second scan on the strip of recent output (last few KiB).
                tail = "".join(session._output)[-4000:]
                m = _URL_RE.search(_strip_ansi(tail))
            if m is not None:
                session.url = m.group(0)
                session._url_event.set()

        if session.token is None:
            m = _TOKEN_RE.search(chunk)
            if m is None:
                tail = "".join(session._output)[-4000:]
                m = _TOKEN_RE.search(_strip_ansi(tail))
            if m is not None:
                session.token = m.group(0)
                session._token_event.set()

    async def _cleanup(self, session: LoginSession) -> None:
        """Terminate subprocess + close PTY + cancel reader. Idempotent."""
        proc = session._process
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass

        if session._reader_task and not session._reader_task.done():
            session._reader_task.cancel()
            try:
                await session._reader_task
            except (asyncio.CancelledError, Exception):
                pass

        if session._master_fd is not None:
            try:
                os.close(session._master_fd)
            except OSError:
                pass
            session._master_fd = None


# --------------------------------------------------------------------------- module-level helpers


# 4 KiB at a time — enough to catch the URL line without spinning too tight.
_PTY_READ_SIZE = 4096


def _read_chunk(fd: int) -> bytes:
    """Blocking read of up to _PTY_READ_SIZE bytes. Returns b'' on EOF."""
    try:
        return os.read(fd, _PTY_READ_SIZE)
    except OSError:
        return b""


# Minimal ANSI-CSI sequence stripper — enough to recover URLs and tokens
# split by terminal control codes. Not a full ANSI parser.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# Singleton — wired into the FastAPI lifespan in main.py.
oauth_login_manager = OAuthLoginManager()
