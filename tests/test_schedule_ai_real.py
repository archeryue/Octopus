"""Real-CLI integration test for natural-language schedule parsing.

Skipped unless the `claude` binary is on PATH (mirrors test_backend_*_real.py).
Exercises the actual one-shot parse end-to-end — proves run_claude_oneshot +
JSON extraction + validation work against the live CLI, not just mocks."""

import shutil

import pytest

from server.schedule_ai import parse_schedule_text

pytestmark = pytest.mark.skipif(
    shutil.which("claude") is None, reason="needs the claude CLI on PATH"
)


@pytest.mark.asyncio
async def test_real_ai_parses_morning_into_cron():
    parsed = await parse_schedule_text(
        "summarize my unread email every morning at 9am",
        timezone="America/Los_Angeles",
        now_iso="2026-05-21T14:00:00",
    )
    # "every morning at 9am" → a daily cron at hour 9 in the local tz.
    assert parsed.cron is not None, parsed
    fields = parsed.cron.split()
    assert len(fields) == 5
    assert fields[1] == "9"  # hour field
    assert parsed.timezone == "America/Los_Angeles"
    assert "email" in parsed.prompt.lower()
    assert parsed.recurrence_label  # non-empty human label


@pytest.mark.asyncio
async def test_real_ai_parses_interval():
    parsed = await parse_schedule_text(
        "check the build status every 15 minutes",
        timezone="UTC",
        now_iso="2026-05-21T14:00:00",
    )
    # Either a 15-minute interval or a */15 cron is acceptable.
    if parsed.interval_seconds is not None:
        assert parsed.interval_seconds == 900
    else:
        assert parsed.cron is not None
    assert "build" in parsed.prompt.lower()
