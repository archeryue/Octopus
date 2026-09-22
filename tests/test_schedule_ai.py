"""Unit tests for natural-language schedule parsing (server/schedule_ai.py).

The pure helpers are tested directly; the AI path is tested with an injected
fake runner so no real `claude` CLI is needed."""

import pytest

from server import schedule_ai
from server.schedule_ai import (
    ScheduleParseError,
    build_explicit_schedule,
    derive_name,
    describe_cron,
    extract_json,
    format_interval,
    local_timezone,
    normalize_timezone,
    parse_interval_token,
    parse_rigid,
    parse_schedule_text,
    recurrence_label_for,
    resolve_timezone,
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
    assert p.run_at is None


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
    assert p.run_at is None


def test_validate_once_naive_datetime():
    """kind=once with a naive datetime gets the default_tz applied."""
    p = validate_parsed(
        {
            "name": "Remind me",
            "prompt": "send reminder",
            "recurrence": {"kind": "once", "run_at_iso": "2030-06-15T15:00:00"},
            "recurrence_label": "Once on June 15 at 3:00 PM",
        },
        default_tz="America/New_York",
        original_text="remind me at 3pm on June 15",
    )
    assert p.run_at is not None
    assert "2030-06-15" in p.run_at
    assert "15:00:00" in p.run_at
    assert p.interval_seconds is None
    assert p.cron is None
    assert p.recurrence_label == "Once on June 15 at 3:00 PM"


def test_validate_once_aware_datetime():
    """kind=once with a timezone-aware ISO string is preserved as-is."""
    p = validate_parsed(
        {
            "name": "One-time task",
            "prompt": "do the thing",
            "recurrence": {"kind": "once", "run_at_iso": "2030-07-04T10:00:00+05:30"},
        },
        default_tz="UTC",
        original_text="do the thing at 10am IST on July 4",
    )
    assert p.run_at is not None
    assert "2030-07-04" in p.run_at
    assert "10:00:00" in p.run_at
    assert p.recurrence_label == "Once at 2030-07-04T10:00:00+05:30"


def test_validate_once_missing_run_at_raises():
    """kind=once without run_at_iso raises ScheduleParseError."""
    with pytest.raises(ScheduleParseError):
        validate_parsed(
            {"prompt": "x", "recurrence": {"kind": "once"}},
            default_tz="UTC",
            original_text="x",
        )


def test_validate_once_invalid_datetime_raises():
    """kind=once with an unparseable datetime raises ScheduleParseError."""
    with pytest.raises(ScheduleParseError):
        validate_parsed(
            {"prompt": "x", "recurrence": {"kind": "once", "run_at_iso": "not-a-date"}},
            default_tz="UTC",
            original_text="x",
        )


@pytest.mark.parametrize(
    "obj",
    [
        {"prompt": "", "recurrence": {"kind": "interval", "interval_seconds": 60}},
        {"prompt": "x", "recurrence": {"kind": "interval", "interval_seconds": 30}},
        {"prompt": "x", "recurrence": {"kind": "cron", "cron": "nonsense"}},
        {"prompt": "x", "recurrence": {"kind": "cron", "cron": "99 99 * * *"}},
        {"prompt": "x", "recurrence": {"kind": "once"}},
        {"prompt": "x", "recurrence": {"kind": "once", "run_at_iso": "not-a-date"}},
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


# --- explicit (structured) recurrence: the agent-facing path ---------------- #
#
# `build_explicit_schedule` is what the schedule MCP tool lands in
# (schedule-tool.md §3): no AI, the caller states the recurrence outright.


def test_local_timezone_is_a_real_zone():
    """Whatever the host is set to, the answer has to load as a zone — the
    default for every agent-created cron depends on it."""
    from zoneinfo import ZoneInfo

    ZoneInfo(local_timezone())


def test_local_timezone_prefers_tz_env(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    assert local_timezone() == "Asia/Shanghai"


def test_local_timezone_ignores_a_bogus_tz_env(monkeypatch):
    """A nonsense TZ falls through to the next source rather than crashing
    every schedule creation on the box."""
    monkeypatch.setenv("TZ", "Mars/Olympus_Mons")
    from zoneinfo import ZoneInfo

    ZoneInfo(local_timezone())


def test_resolve_timezone_rejects_an_unknown_name():
    """Unlike the AI path, an explicitly-passed zone is never silently
    swapped for UTC — that would move every fire without saying so."""
    with pytest.raises(ScheduleParseError) as e:
        resolve_timezone("PST")
    assert "IANA" in str(e.value)


def test_resolve_timezone_defaults_to_the_host():
    assert resolve_timezone(None) == local_timezone()
    assert resolve_timezone("  ") == local_timezone()


@pytest.mark.parametrize(
    "cron,expected",
    [
        ("0 9 * * *", "Every day at 09:00"),
        ("30 18 * * 1", "Every Mon at 18:30"),
        ("0 9 * * 1-5", "Weekdays at 09:00"),
        ("5 4 * * 0,6", "Weekends at 04:05"),
        ("*/15 * * * *", "Every 15 minutes"),
        ("0 */2 * * *", "Every 2 hours"),
        ("0 9 1 * *", "Monthly on day 1 at 09:00"),
    ],
)
def test_describe_cron_common_shapes(cron, expected):
    assert describe_cron(cron) == expected


@pytest.mark.parametrize("cron", ["0 9 1 * 1", "0 9 * 3 *", "0 9 * * MON", "nonsense"])
def test_describe_cron_declines_what_it_would_paraphrase_badly(cron):
    """None, not a wrong sentence: the caller falls back to printing the
    expression itself."""
    assert describe_cron(cron) is None


def test_build_explicit_requires_exactly_one_recurrence():
    with pytest.raises(ScheduleParseError) as e:
        build_explicit_schedule(prompt="x")
    assert "exactly one" in str(e.value)
    with pytest.raises(ScheduleParseError) as e:
        build_explicit_schedule(prompt="x", cron="0 9 * * *", interval_seconds=300)
    assert "cron" in str(e.value) and "interval_seconds" in str(e.value)


def test_build_explicit_requires_a_prompt():
    with pytest.raises(ScheduleParseError):
        build_explicit_schedule(prompt="   ", interval_seconds=300)


def test_build_explicit_interval():
    p = build_explicit_schedule(prompt="check the build", interval_seconds=1800)
    assert (p.interval_seconds, p.cron, p.run_at) == (1800, None, None)
    assert p.recurrence_label == "Every 30m"
    assert p.name == "check the build"
    # An interval has no clock time, so it carries no timezone.
    assert p.timezone is None


def test_build_explicit_interval_floor():
    with pytest.raises(ScheduleParseError) as e:
        build_explicit_schedule(prompt="x", interval_seconds=30)
    assert "60" in str(e.value)


def test_build_explicit_cron_keeps_the_expression_and_labels_it():
    p = build_explicit_schedule(
        prompt="summarize the week",
        name="Weekly wrap",
        cron="0 17 * * 5",
        tz="Asia/Shanghai",
    )
    assert p.cron == "0 17 * * 5"
    assert p.timezone == "Asia/Shanghai"
    assert p.recurrence_label == "Every Fri at 17:00"
    assert p.name == "Weekly wrap"


def test_build_explicit_cron_must_have_five_fields():
    with pytest.raises(ScheduleParseError) as e:
        build_explicit_schedule(prompt="x", cron="0 9 * *")
    assert "5-field" in str(e.value)


def test_build_explicit_cron_must_be_valid():
    with pytest.raises(ScheduleParseError):
        build_explicit_schedule(prompt="x", cron="99 99 * * *")


def test_build_explicit_run_at_attaches_the_zone_and_must_be_future():
    from datetime import datetime, timedelta, timezone as dt_tz

    soon = datetime.now(dt_tz.utc) + timedelta(hours=3)
    p = build_explicit_schedule(
        prompt="ping me", run_at=soon.isoformat(), tz="America/Los_Angeles"
    )
    assert p.run_at is not None and p.cron is None and p.interval_seconds is None
    assert p.recurrence_label.startswith("Once on ")

    past = datetime.now(dt_tz.utc) - timedelta(minutes=1)
    with pytest.raises(ScheduleParseError) as e:
        build_explicit_schedule(prompt="ping me", run_at=past.isoformat())
    assert "past" in str(e.value)


def test_build_explicit_run_at_naive_is_read_in_the_schedule_zone():
    """A wall-clock time with no offset means that time *there* — the whole
    point of carrying a zone."""
    from datetime import datetime, timedelta

    from zoneinfo import ZoneInfo

    local = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)
    p = build_explicit_schedule(
        prompt="ping", run_at=local.replace(tzinfo=None).isoformat(), tz="Asia/Shanghai"
    )
    assert datetime.fromisoformat(p.run_at).utcoffset() == local.utcoffset()


def test_build_explicit_run_at_rejects_nonsense():
    with pytest.raises(ScheduleParseError) as e:
        build_explicit_schedule(prompt="x", run_at="tomorrow afternoon")
    assert "ISO" in str(e.value)


def test_build_explicit_can_skip_the_future_check_for_a_stored_value():
    """Re-reading an existing one-time schedule (a timezone edit, say) must
    not fail on the value it already has."""
    p = build_explicit_schedule(
        prompt="x", run_at="2020-01-01T09:00", require_future=False
    )
    assert p.run_at.startswith("2020-01-01T09:00")
