"""Unit tests for natural-language schedule parsing (server/schedule_ai.py).

The pure helpers are tested directly; the AI path is tested with an injected
fake runner so no real `claude` CLI is needed."""

import pytest

from server import schedule_ai
from server.schedule_ai import (
    ScheduleParseError,
    derive_name,
    extract_json,
    format_interval,
    normalize_timezone,
    parse_interval_token,
    parse_rigid,
    parse_schedule_text,
    recurrence_label_for,
    validate_parsed,
)


# --- interval token + formatting ------------------------------------------- #


@pytest.mark.parametrize(
    "token,expected",
    [("30s", 30), ("15m", 900), ("2h", 7200), ("1d", 86400), ("5", 300), ("  2H ", 7200)],
)
def test_parse_interval_token_ok(token, expected):
    assert parse_interval_token(token) == expected


@pytest.mark.parametrize("token", ["soon", "m", "0", "1.5h", ""])
def test_parse_interval_token_rejects(token):
    assert parse_interval_token(token) is None


@pytest.mark.parametrize(
    "seconds,expected",
    [(86400, "1d"), (7200, "2h"), (2700, "45m"), (90, "90s")],
)
def test_format_interval(seconds, expected):
    assert format_interval(seconds) == expected


def test_derive_name():
    assert derive_name("Check email\nmore") == "Check email"
    assert derive_name("   ") == "Scheduled task"
    long = "a" * 80
    assert derive_name(long).endswith("…") and len(derive_name(long)) <= 48


def test_recurrence_label_for_fallbacks():
    assert recurrence_label_for({"recurrence_label": "Every day at 9 AM"}) == "Every day at 9 AM"
    assert recurrence_label_for({"interval_seconds": 2700, "cron": None}) == "Every 45m"
    assert recurrence_label_for({"cron": "0 9 * * *"}).startswith("Cron")
    assert recurrence_label_for({}) == "—"


def test_normalize_timezone():
    assert normalize_timezone("America/Los_Angeles") == "America/Los_Angeles"
    assert normalize_timezone("Not/AZone") == "UTC"
    assert normalize_timezone(None) == "UTC"


# --- rigid fast path -------------------------------------------------------- #


def test_parse_rigid_interval():
    p = parse_rigid("45m e2e probe")
    assert p is not None
    assert p.interval_seconds == 2700
    assert p.cron is None
    assert p.prompt == "e2e probe"
    assert p.recurrence_label == "Every 45m"


def test_parse_rigid_non_interval_returns_none():
    # First token isn't an interval → defer to the AI path.
    assert parse_rigid("Check my email every morning") is None


def test_parse_rigid_too_short_raises():
    with pytest.raises(ScheduleParseError):
        parse_rigid("30s too fast")


def test_parse_rigid_interval_only_no_prompt():
    assert parse_rigid("30m") is None


# --- JSON extraction -------------------------------------------------------- #


def test_extract_json_fenced():
    obj = extract_json('```json\n{"a": 1}\n```')
    assert obj == {"a": 1}


def test_extract_json_with_prose():
    obj = extract_json('Here you go:\n{"a": 1, "b": "x"}\nHope that helps!')
    assert obj == {"a": 1, "b": "x"}


def test_extract_json_malformed_raises():
    with pytest.raises(ScheduleParseError):
        extract_json("not json at all")
    with pytest.raises(ScheduleParseError):
        extract_json("{not: valid}")


# --- validation ------------------------------------------------------------- #


def test_validate_interval():
    p = validate_parsed(
        {
            "name": "Ping",
            "prompt": "ping the API",
            "recurrence": {"kind": "interval", "interval_seconds": 1800},
        },
        default_tz="UTC",
        original_text="x",
    )
    assert p.interval_seconds == 1800
    assert p.recurrence_label == "Every 30m"


def test_validate_cron():
    p = validate_parsed(
        {
            "name": "Gmail",
            "prompt": "summarize unread email",
            "recurrence": {"kind": "cron", "cron": "0 9 * * *"},
            "recurrence_label": "Every day at 9:00 AM",
        },
        default_tz="America/Los_Angeles",
        original_text="x",
    )
    assert p.cron == "0 9 * * *"
    assert p.timezone == "America/Los_Angeles"
    assert p.recurrence_label == "Every day at 9:00 AM"
    assert p.interval_seconds is None


@pytest.mark.parametrize(
    "obj",
    [
        {"prompt": "", "recurrence": {"kind": "interval", "interval_seconds": 60}},
        {"prompt": "x", "recurrence": {"kind": "interval", "interval_seconds": 30}},
        {"prompt": "x", "recurrence": {"kind": "cron", "cron": "nonsense"}},
        {"prompt": "x", "recurrence": {"kind": "cron", "cron": "99 99 * * *"}},
        {"prompt": "x", "recurrence": {"kind": "weekly"}},
        {"prompt": "x"},
    ],
)
def test_validate_rejects(obj):
    with pytest.raises(ScheduleParseError):
        validate_parsed(obj, default_tz="UTC", original_text="x")


def test_validate_derives_name_when_missing():
    p = validate_parsed(
        {"prompt": "do the thing", "recurrence": {"kind": "interval", "interval_seconds": 60}},
        default_tz="UTC",
        original_text="x",
    )
    assert p.name == "do the thing"


# --- orchestration (rigid vs AI) ------------------------------------------- #


@pytest.mark.asyncio
async def test_parse_schedule_text_rigid_skips_ai():
    called = False

    async def fake_runner(*a, **k):
        nonlocal called
        called = True
        return "{}"

    p = await parse_schedule_text("30m check build", runner=fake_runner)
    assert p.interval_seconds == 1800
    assert called is False  # rigid path took it, no AI call


@pytest.mark.asyncio
async def test_parse_schedule_text_ai_cron():
    async def fake_runner(ctx):
        # The prompt should carry the timezone we passed.
        assert "America/Los_Angeles" in ctx.prompt
        return (
            '```json\n{"name":"Gmail","prompt":"summarize unread email",'
            '"recurrence":{"kind":"cron","cron":"0 9 * * *"},'
            '"recurrence_label":"Every day at 9:00 AM"}\n```'
        )

    p = await parse_schedule_text(
        "summarize my unread email every morning 9am",
        timezone="America/Los_Angeles",
        now_iso="2026-05-21T14:00:00",
        runner=fake_runner,
    )
    assert p.cron == "0 9 * * *"
    assert p.timezone == "America/Los_Angeles"
    assert p.prompt == "summarize unread email"


@pytest.mark.asyncio
async def test_parse_schedule_text_ai_error_propagates():
    async def fake_runner(*a, **k):
        return "the model rambled with no json"

    with pytest.raises(ScheduleParseError):
        await parse_schedule_text("do something clever sometime", runner=fake_runner)


@pytest.mark.asyncio
async def test_parse_schedule_text_empty_raises():
    with pytest.raises(ScheduleParseError):
        await parse_schedule_text("   ")


@pytest.mark.asyncio
async def test_harness_run_oneshot_is_the_default_runner():
    """With no explicit runner, the AI path calls harness.run_oneshot —
    backend-agnostic, the agent's own harness (claude-code or codex)."""

    class FakeHarness:
        called_with = None

        async def run_oneshot(self, ctx):
            FakeHarness.called_with = ctx
            return (
                '{"name":"X","prompt":"do it","recurrence":'
                '{"kind":"interval","interval_seconds":300},"recurrence_label":"every 5m"}'
            )

    p = await parse_schedule_text("something useful every 5 minutes", harness=FakeHarness())
    assert p.interval_seconds == 300
    assert FakeHarness.called_with is not None  # the harness one-shot ran


@pytest.mark.asyncio
async def test_parse_schedule_text_no_harness_no_runner_raises():
    """A free-text (non-rigid) parse with no harness and no runner is an
    explicit error, not a silent fallback to some hardcoded CLI."""
    with pytest.raises(ScheduleParseError):
        await parse_schedule_text("do something clever sometime")
