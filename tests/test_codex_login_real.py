"""Real-CLI integration test for Codex device-auth login.

Skipped unless the `codex` binary is on PATH. Starts a real
`codex login --device-auth`, asserts we scrape a verification URL + one-time
code from its live output, then cancels (no browser authorization happens, so
nothing is actually logged in)."""

import asyncio

import pytest

from server import codex_login
from server.backends.subprocess_jsonl import _which_with_fallback
from server.config import settings

pytestmark = pytest.mark.skipif(
    _which_with_fallback("codex") is None, reason="needs the codex CLI on PATH"
)


@pytest.mark.asyncio
async def test_real_codex_device_login_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "codex_home_dir", str(tmp_path / "codex"))
    mgr = codex_login.CodexLoginManager()
    session = await mgr.start("real-probe")
    try:
        # start() is non-blocking; wait for the drive task to scrape the code.
        await asyncio.wait_for(session._scraped.wait(), 30)
        assert session.verification_url
        assert session.verification_url.startswith("https://")
        assert session.user_code and "-" in session.user_code
        assert session.state == codex_login.CodexLoginState.pending
    finally:
        await mgr.cancel(session.id)
