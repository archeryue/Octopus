"""Natural-language schedule parsing for the `/schedule` command.

Two-tier parse, both harness-agnostic so there's a single source of truth:

  1. Deterministic *rigid* fast path — `<interval> <prompt>` (e.g.
     "30m check email"). No AI, no network, instant. Covers explicit forms.
  2. AI path — a one-shot model call (`harness.run_oneshot`) that turns free
     text like "summarize my gmail unreads every morning 9am" into a structured
     recurrence (cron or interval) + task prompt + human label. Runs on the
     agent's own harness (claude-code or codex) with its resolved credential —
     no hardcoded binary.

The pure helpers (rigid parse, JSON extraction, validation, label
formatting) are unit-tested directly; the one-shot call goes through the
harness, so route/unit tests pass a fake harness (or `runner`).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger

from .harness import HarnessCredential, HarnessOneshotError, OneShotContext

logger = logging.getLogger(__name__)

MIN_INTERVAL_SECONDS = 60

USAGE = (
    "Tell me what to run and when — e.g. "
    '"/schedule summarize my unread email every morning at 9am" or '
    '"/schedule 30m check the build".'
)


class ScheduleParseError(Exception):
    """Raised when text can't be turned into a valid schedule. The message is
    user-facing (surfaced in the chat as a notice / 422 detail)."""


@dataclass
class ParsedSchedule:
    name: str
    prompt: str
    interval_seconds: int | None
    cron: str | None
    timezone: str | None
    recurrence_label: str
    run_at: str | None = None  # ISO datetime for one-time schedules


# --------------------------------------------------------------------------- #
# Formatting / labels
# --------------------------------------------------------------------------- #


def format_interval(seconds: int) -> str:
    """Compact interval like "45m" / "2h" / "1d" (mirrors the old web helper)."""
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def recurrence_label_for(row: dict) -> str:
    """Display label for a schedule row, falling back when not stored (legacy
    interval rows predate the column)."""
    label = (row.get("recurrence_label") or "").strip()
    if label:
        return label
    if row.get("run_at"):
        return f"Once at {row['run_at']}"
    if row.get("cron"):
        return f"Cron: {row['cron']}"
    interval = row.get("interval_seconds")
    if interval:
        return f"Every {format_interval(int(interval))}"
    return "—"


def derive_name(prompt: str) -> str:
    """A short schedule name from its prompt: first line, trimmed, capped."""
    first_line = prompt.split("\n", 1)[0].strip()
    if not first_line:
        return "Scheduled task"
    if len(first_line) <= 48:
        return first_line
    return first_line[:47].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Explicit (structured) recurrence — the agent-facing path
# --------------------------------------------------------------------------- #


def local_timezone() -> str:
    """The host's IANA timezone name.

    The browser sends its own tz with `/schedule`, so a human's "9am" is
    their 9am. An agent calling the schedule tool has no browser, and
    defaulting to UTC would quietly turn "every day at 9am" into 9am UTC —
    on this box, eight hours out. The host clock is the honest default;
    resolution mirrors what the OS itself reads, and UTC is only the answer
    when none of it is legible.
    """
    candidates: list[str] = [(os.environ.get("TZ") or "").strip()]
    try:
        candidates.append(Path("/etc/timezone").read_text().strip())
    except OSError:
        pass
    try:
        link = os.path.realpath("/etc/localtime")
        if "/zoneinfo/" in link:
            candidates.append(link.split("/zoneinfo/", 1)[1])
    except OSError:
        pass
    for name in candidates:
        if not name:
            continue
        try:
            ZoneInfo(name)
            return name
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            continue
    return "UTC"


def resolve_timezone(tz: str | None) -> str:
    """Validate an explicitly-given IANA zone, or fall back to the host's.

    Unlike `normalize_timezone` (which absorbs whatever the AI emitted), a
    name passed deliberately and not recognised is an error: silently
    substituting UTC would move every fire without saying so.
    """
    if not (tz or "").strip():
        return local_timezone()
    tz = tz.strip()
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise ScheduleParseError(
            f"Unknown timezone {tz!r} — use an IANA name like "
            "'America/Los_Angeles' or 'Asia/Shanghai'."
        )
    return tz


_DOW_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def _dow_list(field: str) -> list[int] | None:
    """The day-of-week field as sorted unique 0-6 ints, or None if it uses a
    form we don't put into words (steps, names, anything unexpected)."""
    days: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if "-" in part:
            lo, _, hi = part.partition("-")
            if not (lo.isdigit() and hi.isdigit()):
                return None
            span = range(int(lo), int(hi) + 1)
        elif part.isdigit():
            span = range(int(part), int(part) + 1)
        else:
            return None
        for d in span:
            if not 0 <= d <= 7:
                return None
            days.add(0 if d == 7 else d)
    return sorted(days) or None


def describe_cron(cron: str) -> str | None:
    """A crontab expression in words, for the common shapes — or None when it
    is one we'd only paraphrase badly.

    The label is what the sidebar and the Schedules page show; "0 9 * * 1-5"
    printed twice (once as the cron, once as its own label) tells the user
    nothing they didn't already see.
    """
    parts = cron.split()
    if len(parts) != 5:
        return None
    minute, hour, dom, month, dow = parts
    if month != "*" or (dom != "*" and dow != "*"):
        return None

    if dom == "*" and dow == "*":
        if hour == "*" and minute.startswith("*/") and minute[2:].isdigit():
            return f"Every {int(minute[2:])} minutes"
        if minute.isdigit() and hour.startswith("*/") and hour[2:].isdigit():
            every = f"Every {int(hour[2:])} hours"
            return every if int(minute) == 0 else f"{every} at :{int(minute):02d}"

    if not (minute.isdigit() and hour.isdigit()):
        return None
    at = f"{int(hour):02d}:{int(minute):02d}"
    if dom != "*":
        return f"Monthly on day {int(dom)} at {at}" if dom.isdigit() else None
    if dow == "*":
        return f"Every day at {at}"
    days = _dow_list(dow)
    if days is None:
        return None
    if days == [1, 2, 3, 4, 5]:
        return f"Weekdays at {at}"
    if days == [0, 6]:
        return f"Weekends at {at}"
    return f"Every {', '.join(_DOW_NAMES[d] for d in days)} at {at}"


def build_explicit_schedule(
    *,
    prompt: str,
    name: str | None = None,
    cron: str | None = None,
    interval_seconds: int | None = None,
    run_at: str | None = None,
    tz: str | None = None,
    now: datetime | None = None,
    require_future: bool = True,
) -> ParsedSchedule:
    """Validate a recurrence that was stated outright, with no AI in the loop.

    `/schedule` has to read English because a human typed it. A model calling
    the schedule tool doesn't: it can write the crontab field itself, and
    having it do so keeps a tool call free of a second model call — no extra
    latency, no extra cost, no second parse to fail. The checks are the ones
    `validate_parsed` runs on the AI's JSON, applied to arguments instead, and
    every message is written to be read by whoever passed the bad value.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise ScheduleParseError("`prompt` is required — say what should run.")

    given = [
        key
        for key, value in (
            ("cron", cron),
            ("interval_seconds", interval_seconds),
            ("run_at", run_at),
        )
        if value not in (None, "")
    ]
    if len(given) != 1:
        raise ScheduleParseError(
            "Pass exactly one of `cron`, `interval_seconds` or `run_at` "
            f"(got: {', '.join(given) or 'none'})."
        )
    label = (name or "").strip() or derive_name(prompt)

    if given == ["interval_seconds"]:
        seconds = _coerce_int(interval_seconds)
        if seconds is None or seconds < MIN_INTERVAL_SECONDS:
            raise ScheduleParseError(
                f"`interval_seconds` must be a whole number of seconds, "
                f"at least {MIN_INTERVAL_SECONDS}."
            )
        return ParsedSchedule(
            name=label,
            prompt=prompt,
            interval_seconds=seconds,
            cron=None,
            timezone=None,
            recurrence_label=f"Every {format_interval(seconds)}",
        )

    zone = resolve_timezone(tz)

    if given == ["cron"]:
        expr = str(cron).strip()
        if len(expr.split()) != 5:
            raise ScheduleParseError(
                "`cron` must be a 5-field crontab expression: "
                "'<minute> <hour> <day-of-month> <month> <day-of-week>' "
                "(e.g. '0 9 * * 1-5' for weekdays at 9am)."
            )
        try:
            CronTrigger.from_crontab(expr, timezone=ZoneInfo(zone))
        except (ValueError, ZoneInfoNotFoundError) as e:
            raise ScheduleParseError(f"`cron` isn't a valid crontab expression: {e}")
        return ParsedSchedule(
            name=label,
            prompt=prompt,
            interval_seconds=None,
            cron=expr,
            timezone=zone,
            recurrence_label=describe_cron(expr) or f"Cron {expr}",
        )

    try:
        when = datetime.fromisoformat(str(run_at).strip())
    except ValueError:
        raise ScheduleParseError(
            "`run_at` must be an ISO datetime like '2026-09-22T15:00' "
            "(local to the schedule's timezone unless it carries an offset)."
        )
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo(zone))
    if require_future:
        reference = now or datetime.now(dt_timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=ZoneInfo(zone))
        # A one-time schedule in the past never fires — it is deleted on the
        # next boot as a missed run. Say so now rather than accept it.
        if when <= reference + timedelta(seconds=5):
            raise ScheduleParseError(
                f"`run_at` is in the past ({when.isoformat()}); a one-time "
                "schedule has to be in the future."
            )
    return ParsedSchedule(
        name=label,
        prompt=prompt,
        interval_seconds=None,
        cron=None,
        timezone=zone,
        recurrence_label=f"Once on {when:%Y-%m-%d} at {when:%H:%M}",
        run_at=when.isoformat(),
    )


# --------------------------------------------------------------------------- #
# Rigid fast path: "<interval> <prompt>"
# --------------------------------------------------------------------------- #

_INTERVAL_RE = re.compile(r"^(\d+)(s|m|h|d)?$", re.IGNORECASE)
_RIGID_RE = re.compile(r"^(\S+)\s+([\s\S]+)$")
_UNIT_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_interval_token(token: str) -> int | None:
    """`30s`/`15m`/`2h`/`1d`, or a bare number = minutes → seconds; else None."""
    m = _INTERVAL_RE.match(token.strip())
    if not m:
        return None
    n = int(m.group(1))
    if n <= 0:
        return None
    return n * _UNIT_MULT[(m.group(2) or "m").lower()]


def parse_rigid(text: str) -> ParsedSchedule | None:
    """Parse the explicit `<interval> <prompt>` form. Returns None when the
    first token isn't an interval (so the caller falls through to AI). Raises
    ScheduleParseError when it *is* an interval but invalid (too short)."""
    m = _RIGID_RE.match(text.strip())
    if not m:
        return None
    seconds = parse_interval_token(m.group(1))
    if seconds is None:
        return None  # not the rigid form → let the AI path handle it
    if seconds < MIN_INTERVAL_SECONDS:
        raise ScheduleParseError("Minimum interval is 1m (60s).")
    prompt = m.group(2).strip()
    if not prompt:
        return None
    return ParsedSchedule(
        name=derive_name(prompt),
        prompt=prompt,
        interval_seconds=seconds,
        cron=None,
        timezone=None,
        recurrence_label=f"Every {format_interval(seconds)}",
    )


# --------------------------------------------------------------------------- #
# AI path
# --------------------------------------------------------------------------- #


def normalize_timezone(tz: str | None) -> str:
    """Validate an IANA tz name; fall back to UTC."""
    if not tz:
        return "UTC"
    try:
        ZoneInfo(tz)
        return tz
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return "UTC"


def build_parse_prompt(text: str, now_iso: str, tz: str) -> str:
    return (
        "You convert a natural-language scheduling request into a JSON object.\n\n"
        f"Current local time: {now_iso}\n"
        f"Local timezone: {tz}\n\n"
        "Return ONLY a JSON object (no prose, no markdown fences) with keys:\n"
        '- "name": a short label (max ~6 words) for the schedule.\n'
        '- "prompt": the task to run, as a clear instruction with the '
        "timing/recurrence words removed.\n"
        '- "recurrence": exactly one of:\n'
        '    {"kind":"once","run_at_iso":"<YYYY-MM-DDTHH:MM:SS>"} — for a '
        "single one-time run at a specific date/time (\"at 3pm tomorrow\", "
        "\"next Monday at 2pm\"). The datetime is in the LOCAL timezone above. "
        "Use this ONLY when the user clearly wants a single execution.\n"
        '    {"kind":"cron","cron":"<min> <hour> <day-of-month> <month> '
        '<day-of-week>"} — for recurring clock-time / day-of-week schedules '
        "(every morning, weekdays 9am, every Monday). Standard 5-field crontab "
        "interpreted in the LOCAL timezone above; day-of-week 0=Sunday..6="
        "Saturday.\n"
        '    {"kind":"interval","interval_seconds":<int>} — for "every N '
        'minutes/hours" with no specific clock time. Minimum 60.\n'
        '- "recurrence_label": a short human description, e.g. "Every day at '
        '9:00 AM" or "Once on Monday at 2:00 PM".\n\n'
        f"Request: {text}"
    )


def extract_json(model_text: str) -> dict:
    """Pull the JSON object out of the model's reply (tolerates ```json fences
    and surrounding prose)."""
    s = model_text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ScheduleParseError("The AI returned an unexpected response. Try rephrasing.")
    try:
        obj = json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        raise ScheduleParseError("The AI returned malformed JSON. Try rephrasing.")
    if not isinstance(obj, dict):
        raise ScheduleParseError("The AI returned an unexpected response. Try rephrasing.")
    return obj


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def validate_parsed(obj: dict, *, default_tz: str, original_text: str) -> ParsedSchedule:
    prompt = str(obj.get("prompt") or "").strip()
    if not prompt:
        raise ScheduleParseError("Couldn't work out the task to run. Try rephrasing.")
    name = str(obj.get("name") or "").strip() or derive_name(prompt or original_text)
    label = str(obj.get("recurrence_label") or "").strip()

    rec = obj.get("recurrence")
    if not isinstance(rec, dict):
        raise ScheduleParseError("Couldn't work out the timing. Try e.g. \"every day at 9am\".")
    kind = rec.get("kind")

    if kind == "interval":
        seconds = _coerce_int(rec.get("interval_seconds"))
        if seconds is None or seconds < MIN_INTERVAL_SECONDS:
            raise ScheduleParseError("Minimum interval is 1m (60s).")
        return ParsedSchedule(
            name=name,
            prompt=prompt,
            interval_seconds=seconds,
            cron=None,
            timezone=None,
            recurrence_label=label or f"Every {format_interval(seconds)}",
        )

    if kind == "once":
        run_at_str = str(rec.get("run_at_iso") or "").strip()
        if not run_at_str:
            raise ScheduleParseError(
                "Couldn't work out the one-time date/time. Try rephrasing."
            )
        tz = normalize_timezone(default_tz)
        try:
            dt = datetime.fromisoformat(run_at_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo(tz))
            run_at = dt.isoformat()
        except (ValueError, ZoneInfoNotFoundError):
            raise ScheduleParseError(
                "Couldn't work out the one-time date/time. Try rephrasing."
            )
        return ParsedSchedule(
            name=name,
            prompt=prompt,
            interval_seconds=None,
            cron=None,
            timezone=tz,
            recurrence_label=label or f"Once at {run_at_str}",
            run_at=run_at,
        )

    if kind == "cron":
        cron = str(rec.get("cron") or "").strip()
        if len(cron.split()) != 5:
            raise ScheduleParseError("Couldn't work out the timing. Try rephrasing.")
        tz = normalize_timezone(default_tz)
        try:
            CronTrigger.from_crontab(cron, timezone=ZoneInfo(tz))
        except (ValueError, ZoneInfoNotFoundError):
            raise ScheduleParseError("Couldn't work out the timing. Try rephrasing.")
        return ParsedSchedule(
            name=name,
            prompt=prompt,
            interval_seconds=None,
            cron=cron,
            timezone=tz,
            recurrence_label=label or f"Cron {cron}",
        )

    raise ScheduleParseError("Couldn't work out the timing. Try e.g. \"every day at 9am\".")


# Map a harness one-shot failure code to a user-facing schedule message.
_ONESHOT_ERROR_MESSAGES = {
    "not_found": (
        "Natural-language scheduling needs the agent's CLI installed. Use the "
        'explicit form instead, e.g. "/schedule 30m check the build".'
    ),
    "timeout": 'The AI took too long to read that. Try rephrasing or use "30m …".',
    "failed": 'Couldn\'t reach the AI to read that. Try the explicit "30m …" form.',
    "bad_output": "The AI returned an unexpected response. Try rephrasing.",
    "empty": "The AI returned an empty response. Try rephrasing.",
}


async def parse_schedule_text(
    text: str,
    *,
    harness=None,
    timezone: str | None = None,
    now_iso: str | None = None,
    model: str | None = None,
    credential: HarnessCredential | None = None,
    working_dir: str | None = None,
    runner=None,
) -> ParsedSchedule:
    """Turn free text into a ParsedSchedule. Tries the rigid form first (no
    AI), then the AI path via `harness.run_oneshot`. `runner` (an async
    callable taking an OneShotContext) defaults to `harness.run_oneshot`, so
    tests can pass a fake runner or a fake harness instead of a real CLI."""
    text = (text or "").strip()
    if not text:
        raise ScheduleParseError(USAGE)

    rigid = parse_rigid(text)
    if rigid is not None:
        return rigid

    run = runner or (harness.run_oneshot if harness is not None else None)
    if run is None:
        raise ScheduleParseError(
            "Natural-language scheduling isn't available for this agent's harness. "
            'Use the explicit form, e.g. "/schedule 30m check the build".'
        )
    tz = normalize_timezone(timezone)
    now = now_iso or datetime.now(ZoneInfo(tz)).isoformat(timespec="minutes")
    prompt = build_parse_prompt(text, now, tz)
    ctx = OneShotContext(
        prompt=prompt, model=model, credential=credential, working_dir=working_dir
    )
    try:
        model_text = await run(ctx)
    except HarnessOneshotError as e:
        raise ScheduleParseError(
            _ONESHOT_ERROR_MESSAGES.get(e.code, _ONESHOT_ERROR_MESSAGES["failed"])
        )
    return validate_parsed(extract_json(model_text), default_tz=tz, original_text=text)
