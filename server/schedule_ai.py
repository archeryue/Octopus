"""Natural-language schedule parsing for the `/schedule` command.

Two-tier parse, both backend-side so there's a single source of truth:

  1. Deterministic *rigid* fast path — `<interval> <prompt>` (e.g.
     "30m check email"). No AI, no network, instant. Covers explicit forms.
  2. AI path — a one-shot `claude --print` call that turns free text like
     "summarize my gmail unreads every morning 9am" into a structured
     recurrence (cron or interval) + task prompt + human label. Reuses the
     agent's Claude auth.

The pure helpers (rigid parse, JSON extraction, validation, label
formatting) are unit-tested directly; the subprocess call (`run_claude_oneshot`)
is injectable so route/unit tests don't need the real CLI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger

from .backends import BackendCredential

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
        '- "prompt": the task to run on each fire, as a clear instruction with '
        "the timing/recurrence words removed.\n"
        '- "recurrence": exactly one of:\n'
        '    {"kind":"cron","cron":"<min> <hour> <day-of-month> <month> '
        '<day-of-week>"} — for clock-time / day-of-week schedules (every '
        "morning, weekdays 9am, every Monday). Standard 5-field crontab "
        "interpreted in the LOCAL timezone above; day-of-week 0=Sunday..6="
        "Saturday.\n"
        '    {"kind":"interval","interval_seconds":<int>} — for "every N '
        'minutes/hours" with no specific clock time. Minimum 60.\n'
        '- "recurrence_label": a short human description, e.g. "Every day at '
        '9:00 AM".\n\n'
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


async def run_claude_oneshot(
    prompt: str,
    *,
    model: str | None = None,
    credential: BackendCredential | None = None,
    working_dir: str | None = None,
    binary: str = "claude",
    timeout: float = 90.0,
) -> str:
    """One-shot, tool-free `claude --print --output-format=json` call. Returns
    the model's text (the `result` field). Mirrors how the claude-code backend
    applies a resolved credential to the subprocess env."""
    argv = [binary, "--print", "--output-format=json"]
    if model:
        argv += ["--model", model]
    argv += ["--", prompt]

    env = os.environ.copy()
    if credential is not None:
        if credential.auth_type == "api_key":
            env["ANTHROPIC_API_KEY"] = credential.secret
        elif credential.auth_type == "oauth":
            env["CLAUDE_CODE_OAUTH_TOKEN"] = credential.secret

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=working_dir or os.getcwd(),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise ScheduleParseError(
            "Natural-language scheduling needs the Claude CLI. Use the explicit "
            'form instead, e.g. "/schedule 30m check the build".'
        )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ScheduleParseError("The AI took too long to read that. Try rephrasing or use \"30m …\".")

    if proc.returncode != 0:
        logger.warning("schedule AI parse exited %s: %s", proc.returncode, err.decode()[:300])
        raise ScheduleParseError("Couldn't reach the AI to read that. Try the explicit \"30m …\" form.")
    try:
        data = json.loads(out.decode())
    except json.JSONDecodeError:
        raise ScheduleParseError("The AI returned an unexpected response. Try rephrasing.")
    text = data.get("result")
    if not isinstance(text, str) or not text.strip():
        raise ScheduleParseError("The AI returned an empty response. Try rephrasing.")
    return text


async def parse_schedule_text(
    text: str,
    *,
    timezone: str | None = None,
    now_iso: str | None = None,
    model: str | None = None,
    credential: BackendCredential | None = None,
    working_dir: str | None = None,
    runner=None,
) -> ParsedSchedule:
    """Turn free text into a ParsedSchedule. Tries the rigid form first (no
    AI), then the AI path. `runner` defaults to `run_claude_oneshot`, looked up
    at call time so tests can monkeypatch it (or pass one explicitly)."""
    text = (text or "").strip()
    if not text:
        raise ScheduleParseError(USAGE)

    rigid = parse_rigid(text)
    if rigid is not None:
        return rigid

    run = runner or run_claude_oneshot
    tz = normalize_timezone(timezone)
    now = now_iso or datetime.now(ZoneInfo(tz)).isoformat(timespec="minutes")
    prompt = build_parse_prompt(text, now, tz)
    model_text = await run(
        prompt, model=model, credential=credential, working_dir=working_dir
    )
    return validate_parsed(extract_json(model_text), default_tz=tz, original_text=text)
