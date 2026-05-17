"""Tests for the OAuth login orchestrator.

Uses a fake `setup-token` script (tests/_fixtures/fake_setup_token.py)
so we don't hit the real Anthropic OAuth flow. The orchestrator's PTY
handling, URL extraction, and code submission paths are exercised
end-to-end against a real subprocess.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from server.oauth_login import (
    LoginState,
    OAuthLoginManager,
)

FAKE = Path(__file__).parent / "_fixtures" / "fake_setup_token.py"


class _ScriptedManager(OAuthLoginManager):
    """Manager that spawns the fake setup-token script instead of the real
    `claude` binary. `mode` is the first argv to the fake script."""

    def __init__(self, mode: str = "ok") -> None:
        super().__init__()
        self._mode = mode

    async def start(self):  # type: ignore[override]
        # Bypass the parent's `which(claude)` resolution; spawn our fake
        # directly. We otherwise reuse the parent implementation by calling
        # into a slightly-rewritten copy. To keep test surface small, we
        # patch the binary lookup via a subclass attribute.
        return await super().start()

    # Override _spawn-time binary resolution by monkeypatching shutil.which
    # at call time in the test, not here. (Subclassing the binary path
    # would require touching the parent's arg list — easier to monkey.)


async def _run_with_fake(monkeypatch, mode: str) -> OAuthLoginManager:
    """Build a manager that thinks `claude` is our fake script + `setup-token`."""
    import shutil
    mgr = OAuthLoginManager()
    # `which("claude")` returns the fake script path; that path is the one
    # passed to create_subprocess_exec.
    monkeypatch.setattr(shutil, "which", lambda name: str(FAKE) if name == "claude" else None)
    # The orchestrator calls `binary` then "setup-token" as argv. Our fake
    # ignores "setup-token" (it uses argv[1] for mode), so we additionally
    # monkeypatch create_subprocess_exec to swap argv.
    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(*argv, **kwargs):
        # Replace ["<fake>", "setup-token"] with ["<python>", "<fake>", "<mode>"]
        new_argv = [sys.executable, str(FAKE), mode]
        return await real_exec(*new_argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return mgr


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ok_flow_yields_url_then_token(monkeypatch):
    mgr = await _run_with_fake(monkeypatch, "ok")
    session = await mgr.start()
    assert session.state == LoginState.awaiting_code
    assert session.url is not None
    assert session.url.startswith("https://claude.ai/oauth/authorize")
    assert "client_id=fake" in session.url

    session = await mgr.submit_code(session.id, "any-code-the-user-pastes")
    assert session.state == LoginState.success
    assert session.token is not None
    assert session.token.startswith("sk-ant-fake-")


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subprocess_dies_before_url_raises(monkeypatch):
    mgr = await _run_with_fake(monkeypatch, "no-url")
    # Subprocess exits without printing a URL — start() must surface that
    # cleanly instead of returning a half-finished session.
    with pytest.raises(RuntimeError, match="exited before printing an authorize URL"):
        await mgr.start()


@pytest.mark.asyncio
async def test_bad_code_yields_error_no_token(monkeypatch):
    mgr = await _run_with_fake(monkeypatch, "bad-code")
    session = await mgr.start()
    # Use a wrong code — fake script exits 1 without producing token.
    with pytest.raises(RuntimeError):
        await mgr.submit_code(session.id, "wrong-code")
    assert mgr.get(session.id).state == LoginState.error


@pytest.mark.asyncio
async def test_token_timeout_raises(monkeypatch):
    """Subprocess hangs after receiving code — submit_code must time out
    cleanly, not block forever."""
    from server import oauth_login as _ol

    # Shrink the token timeout so the test doesn't take 60s.
    monkeypatch.setattr(_ol, "_TOKEN_TIMEOUT_S", 2.0)

    mgr = await _run_with_fake(monkeypatch, "hang-token")
    session = await mgr.start()
    with pytest.raises(RuntimeError, match="Timed out .* waiting for token"):
        await mgr.submit_code(session.id, "any")
    assert mgr.get(session.id).state == LoginState.error


# ---------------------------------------------------------------------------
# Cancel + lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_kills_in_flight_login(monkeypatch):
    mgr = await _run_with_fake(monkeypatch, "hang-token")
    session = await mgr.start()
    assert session.state == LoginState.awaiting_code
    await mgr.cancel(session.id)
    assert mgr.get(session.id).state == LoginState.cancelled
    # Subprocess should already be gone — second cancel is a no-op.
    await mgr.cancel(session.id)


@pytest.mark.asyncio
async def test_submit_code_unknown_id_raises(monkeypatch):
    mgr = OAuthLoginManager()
    with pytest.raises(KeyError):
        await mgr.submit_code("nope", "x")


@pytest.mark.asyncio
async def test_submit_code_wrong_state_raises(monkeypatch):
    mgr = await _run_with_fake(monkeypatch, "ok")
    session = await mgr.start()
    # Drive it to success
    await mgr.submit_code(session.id, "any")
    # Second submit should refuse — state is no longer awaiting_code
    with pytest.raises(RuntimeError, match="cannot accept code"):
        await mgr.submit_code(session.id, "again")


@pytest.mark.asyncio
async def test_shutdown_cancels_all_in_flight(monkeypatch):
    mgr = await _run_with_fake(monkeypatch, "hang-token")
    s1 = await mgr.start()
    # Second hung session — start() is independent of which one we kill.
    s2 = await mgr.start()
    assert s1.state == LoginState.awaiting_code
    assert s2.state == LoginState.awaiting_code
    await mgr.shutdown()
    assert mgr.get(s1.id).state == LoginState.cancelled
    assert mgr.get(s2.id).state == LoginState.cancelled


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_claude_binary_raises(monkeypatch):
    import shutil
    # Simulate `claude` not on PATH and not in any fallback dir
    monkeypatch.setattr(shutil, "which", lambda name: None)
    from server.backends import subprocess_jsonl
    monkeypatch.setattr(subprocess_jsonl, "_which_with_fallback", lambda name: None)
    mgr = OAuthLoginManager()
    with pytest.raises(FileNotFoundError, match="not found on PATH"):
        await mgr.start()
