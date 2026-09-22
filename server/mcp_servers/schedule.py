"""MCP stdio server: an agent's own schedules (schedule-tool.md).

Octopus has had durable scheduling since agent-refactor.md — APScheduler,
a `schedules` table, a real turn per fire — but only a human could set one
up, by typing `/schedule …` in the chat. An agent asked to "check this every
morning" had no way to arrange that for itself. These four tools are that
way; the full MCP names are `mcp__schedule__<tool>`:

  - `create(prompt, …)` — a new schedule for THIS agent. The recurrence is
    stated outright (`cron`, `interval_seconds` or `run_at`), not in English:
    the caller is a model, it can write a crontab field itself, and doing so
    keeps the call free of the second model round-trip `/schedule`'s
    natural-language parse needs.
  - `list()` — this agent's schedules, with ids and next fire times.
  - `update(schedule_id, …)` — pause/resume, re-word, or re-time an existing
    one. Setting a recurrence replaces whichever one it had.
  - `delete(schedule_id)` — remove it.

Channel: this process is a child of the harness CLI (claude / codex), not of
Octopus's FastAPI server, so it calls back over HTTP with the injected env —
OCTOPUS_API_BASE / OCTOPUS_AUTH_TOKEN / OCTOPUS_SESSION_ID — exactly like
bg / ask / ask_agent / research. The session id is not a tool parameter: it
is what scopes every call to the agent that owns this conversation, and the
model is not asked to get that right.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s schedule-mcp %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP("octopus-schedule")

_TIMEOUT = 15.0


def _required_env(name: str) -> str | None:
    v = os.environ.get(name)
    if not v:
        logger.error("Required env var %s not set", name)
    return v


def _context() -> tuple[str, str, dict[str, str]] | None:
    """(api base, session id, auth headers), or None when any is missing."""
    api = _required_env("OCTOPUS_API_BASE")
    sid = _required_env("OCTOPUS_SESSION_ID")
    tok = _required_env("OCTOPUS_AUTH_TOKEN")
    if not (api and sid and tok):
        return None
    return api, sid, {"Authorization": f"Bearer {tok}"}


def _error(resp: httpx.Response, what: str) -> str:
    """One sentence the model can act on. A 422 carries our own validation
    message (which says what to pass instead), so it is quoted verbatim."""
    detail: Any = resp.text[:300]
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("detail"):
            detail = body["detail"]
    except ValueError:
        pass
    if resp.status_code == 422:
        return f"Couldn't {what}: {detail}"
    if resp.status_code == 404:
        return f"Couldn't {what}: {detail} (404)."
    return f"Error ({resp.status_code}) trying to {what}: {detail}"


def _describe(row: dict[str, Any]) -> str:
    """One line per schedule: id, name, when, state, next fire, where it runs."""
    bits = [f"[{row.get('id')}] {row.get('name') or 'Scheduled task'}"]
    label = (row.get("recurrence_label") or "").strip()
    if row.get("cron"):
        label = f"{label} ({row['cron']}"
        label += f", {row['timezone']})" if row.get("timezone") else ")"
    bits.append(label or "—")
    bits.append("active" if row.get("enabled") else "paused")
    if row.get("next_run_at"):
        bits.append(f"next {row['next_run_at']}")
    bits.append(
        "fires into this conversation"
        if row.get("origin_session_id")
        else "fires in its own session"
    )
    return " · ".join(bits)


@mcp.tool(name="create")
def create_schedule(
    prompt: str,
    name: str | None = None,
    cron: str | None = None,
    interval_seconds: int | None = None,
    run_at: str | None = None,
    timezone: str | None = None,
    in_session: bool = True,
) -> str:
    """Schedule a task for yourself — a prompt Octopus will send you later,
    once or on a repeat, whether or not anyone is at the keyboard.

    Use it whenever the user asks for something recurring or deferred:
    "check the build every morning", "remind me in two hours", "every Monday,
    summarize the week". Set it up, confirm what you set up, and end your turn
    — the run happens later, on its own.

    Pass EXACTLY ONE recurrence:
      - `cron="0 9 * * 1-5"` — 5-field crontab, `<minute> <hour> <day-of-month>
        <month> <day-of-week>`, day-of-week 0=Sunday..6=Saturday. This is the
        one to use for clock times: "every morning at 9" is `0 9 * * *`,
        "weekdays at 9" is `0 9 * * 1-5`, "Mondays at 18:30" is `30 18 * * 1`.
      - `interval_seconds=1800` — every N seconds, minimum 60. For "every
        half hour" with no particular clock time.
      - `run_at="2026-09-22T15:00"` — one-time. It fires once and deletes
        itself. Must be in the future.

    When the schedule fires you get a new turn in this session, prefixed
    `[scheduled:<name> — <recurrence>]`, carrying `prompt` as written. Nobody
    is waiting to answer questions at that point, so write `prompt` to be
    self-contained and actionable on its own: "Check the CI status of
    octopus/main and report failures", not "do the thing we discussed".

    Args:
        prompt: What to do when it fires. Self-contained — it is the whole
            message you will receive; this conversation's context may be long
            gone by then.
        name: Short label for the schedules list (e.g. "Morning build
            check"). Defaults to the first line of `prompt`.
        cron: 5-field crontab expression. Interpreted in `timezone`.
        interval_seconds: Repeat every N seconds (minimum 60).
        run_at: ISO datetime for a single run, e.g. "2026-09-22T15:00".
            Local to `timezone` unless it carries an offset.
        timezone: IANA name ("America/Los_Angeles", "Asia/Shanghai").
            Defaults to the host's own timezone, which is normally the
            user's. Pass it when the user names a different one.
        in_session: True (default) — each fire lands in THIS conversation, so
            the user sees it where the schedule was set up. False — each fire
            gets its own throwaway session; use it for noisy background
            routines that shouldn't fill up the chat.

    Returns:
        The schedule's id and when it will first run — tell the user both.
    """
    ctx = _context()
    if ctx is None:
        return "Error: schedule server is misconfigured (env vars missing)."
    api, sid, hdrs = ctx
    if not (prompt or "").strip():
        return "Error: `prompt` must say what to run when the schedule fires."

    body: dict[str, Any] = {"prompt": prompt, "in_session": bool(in_session)}
    for key, value in (
        ("name", name),
        ("cron", cron),
        ("interval_seconds", interval_seconds),
        ("run_at", run_at),
        ("timezone", timezone),
    ):
        if value not in (None, ""):
            body[key] = value

    try:
        r = httpx.post(
            f"{api}/api/sessions/{sid}/schedules",
            json=body,
            headers=hdrs,
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as e:
        return f"Error: failed to reach Octopus to create the schedule: {e}"
    if r.status_code not in (200, 201):
        return _error(r, "create that schedule")

    row = r.json()
    when = row.get("next_run_at") or row.get("run_at") or "—"
    return (
        f"Scheduled. {_describe(row)}\n"
        f"First run: {when}. Change it with "
        f"mcp__schedule__update(schedule_id=\"{row.get('id')}\", …) or remove "
        f"it with mcp__schedule__delete."
    )


@mcp.tool(name="list")
def list_schedules() -> str:
    """Your own schedules — everything set up for this agent, whether you
    created it or the user did with `/schedule`.

    Use it before changing or deleting one (you need the id), when the user
    asks what is scheduled, and to check you aren't about to create a
    duplicate of something that already runs.

    Returns:
        One line per schedule: id, name, recurrence, active/paused, next fire
        time, and whether fires land in this conversation.
    """
    ctx = _context()
    if ctx is None:
        return "Error: schedule server is misconfigured (env vars missing)."
    api, sid, hdrs = ctx
    try:
        r = httpx.get(
            f"{api}/api/sessions/{sid}/schedules", headers=hdrs, timeout=_TIMEOUT
        )
    except httpx.HTTPError as e:
        return f"Error: failed to reach Octopus to list schedules: {e}"
    if r.status_code != 200:
        return _error(r, "list your schedules")

    rows = r.json()
    if not rows:
        return "No schedules set up for this agent yet."
    lines = "\n".join(f"- {_describe(row)}" for row in rows)
    return f"{len(rows)} schedule{'' if len(rows) == 1 else 's'} for this agent:\n{lines}"


@mcp.tool(name="update")
def update_schedule(
    schedule_id: str,
    prompt: str | None = None,
    name: str | None = None,
    cron: str | None = None,
    interval_seconds: int | None = None,
    run_at: str | None = None,
    timezone: str | None = None,
    enabled: bool | None = None,
) -> str:
    """Change one of your schedules: pause or resume it, re-word what it
    runs, or move when it runs.

    Pass only what changes. A recurrence argument REPLACES whatever the
    schedule had (an interval becomes a cron, and so on) — pass at most one of
    `cron` / `interval_seconds` / `run_at`. `timezone` on its own re-reads the
    existing cron in the new zone.

    `enabled=False` pauses without deleting: the schedule stays in the list
    and stops firing. That is usually what "stop doing X for now" means —
    prefer it to `delete` unless the user wants it gone.

    Args:
        schedule_id: From `mcp__schedule__list` (or the id `create` returned).
        prompt: New task text for future fires.
        name: New label.
        cron: New 5-field crontab expression.
        interval_seconds: New repeat interval in seconds (minimum 60).
        run_at: New ISO datetime, turning it into a one-time schedule.
        timezone: New IANA timezone for the cron / one-time run.
        enabled: False to pause, True to resume.

    Returns:
        The schedule as it now stands, including its next fire time.
    """
    ctx = _context()
    if ctx is None:
        return "Error: schedule server is misconfigured (env vars missing)."
    api, sid, hdrs = ctx
    if not (schedule_id or "").strip():
        return "Error: `schedule_id` is required — call mcp__schedule__list first."

    body: dict[str, Any] = {}
    for key, value in (
        ("prompt", prompt),
        ("name", name),
        ("cron", cron),
        ("interval_seconds", interval_seconds),
        ("run_at", run_at),
        ("timezone", timezone),
        ("enabled", enabled),
    ):
        if value not in (None, ""):
            body[key] = value
    if not body:
        return (
            "Error: nothing to change — pass at least one of prompt, name, "
            "cron, interval_seconds, run_at, timezone or enabled."
        )

    try:
        r = httpx.patch(
            f"{api}/api/sessions/{sid}/schedules/{schedule_id.strip()}",
            json=body,
            headers=hdrs,
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as e:
        return f"Error: failed to reach Octopus to update the schedule: {e}"
    if r.status_code != 200:
        return _error(r, f"update schedule {schedule_id}")
    return f"Updated. {_describe(r.json())}"


@mcp.tool(name="delete")
def delete_schedule(schedule_id: str) -> str:
    """Delete one of your schedules for good. It stops firing and leaves the
    list.

    To stop it only for now, use `mcp__schedule__update(schedule_id,
    enabled=False)` instead — a paused schedule can be resumed, a deleted one
    has to be rebuilt.

    Args:
        schedule_id: From `mcp__schedule__list`.

    Returns:
        Confirmation, or why it couldn't be deleted.
    """
    ctx = _context()
    if ctx is None:
        return "Error: schedule server is misconfigured (env vars missing)."
    api, sid, hdrs = ctx
    if not (schedule_id or "").strip():
        return "Error: `schedule_id` is required — call mcp__schedule__list first."

    try:
        r = httpx.delete(
            f"{api}/api/sessions/{sid}/schedules/{schedule_id.strip()}",
            headers=hdrs,
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as e:
        return f"Error: failed to reach Octopus to delete the schedule: {e}"
    if r.status_code not in (200, 204):
        return _error(r, f"delete schedule {schedule_id}")
    return f"Deleted schedule {schedule_id.strip()}. It won't fire again."


if __name__ == "__main__":
    mcp.run()
