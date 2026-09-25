from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import verify_token
from ..models import (
    AgentScheduleRequest,
    CreateScheduleRequest,
    ScheduleInfo,
    UpdateScheduleRequest,
)
from ..schedule_ai import (
    ScheduleParseError,
    build_explicit_schedule,
    recurrence_label_for,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/schedules", tags=["schedules"])

# Session-scoped schedule routes (`/api/sessions/{sid}/schedules`), the
# surface the `schedule` MCP server calls from inside a turn
# (schedule-tool.md §4). Separate router, same module: the scoping rule —
# an agent sees and touches only its own schedules — is derived from the
# session, but everything below it is the same create/update/delete code
# the `/api/schedules` routes use.
session_router = APIRouter(prefix="/api/sessions", tags=["schedules"])

# Injected at startup via app.state
_db = None
_runner = None


def _get_db():
    assert _db is not None
    return _db


def _get_runner():
    assert _runner is not None
    return _runner


def to_schedule_info(row: dict) -> ScheduleInfo:
    """Build the API model from a DB/row dict, always populating the
    human-readable recurrence label (derived for legacy rows that predate it)
    and the live next-fire time from the scheduler."""
    next_run_at = None
    if _runner is not None:
        next_run_at = _runner.next_run_at(row["id"])
    return ScheduleInfo(
        **{
            **row,
            "recurrence_label": recurrence_label_for(row),
            "next_run_at": next_run_at,
        }
    )


async def _broadcast_change() -> None:
    """Tell every open client the schedule list moved.

    A browser refetches after its own edit, so this exists for the changes it
    didn't make: another tab, and — the reason it was added — an agent setting
    a schedule for itself mid-conversation. The payload is deliberately empty;
    one code path reloads the list however it changed.
    """
    from ..session_manager import session_manager

    try:
        await session_manager._broadcast({"type": "schedules_changed"})
    except Exception:  # pragma: no cover - a dead socket must not fail a write
        logger.debug("schedules_changed broadcast failed", exc_info=True)


async def create_schedule_for_agent(
    agent_id: str,
    name: str,
    prompt: str,
    *,
    interval_seconds: int | None = None,
    cron: str | None = None,
    tz: str | None = None,
    recurrence_label: str | None = None,
    origin_session_id: str | None = None,
    run_at: str | None = None,
) -> dict:
    """Persist a schedule owned by `agent_id` and register its job. Recurrence
    is one of `interval_seconds`, `cron` (+`timezone`), or `run_at` (ISO
    datetime, fires once and auto-deletes). Shared by the standalone
    `/api/schedules` route, the agent-scoped `/api/agents/{id}/schedules`
    route, and the natural-language `from_text` route. `origin_session_id` (the
    session the command was issued from) makes each fire append into that
    conversation instead of a throwaway session."""
    schedule_id = uuid.uuid4().hex[:12]
    now = datetime.now(UTC).isoformat()
    label = recurrence_label or recurrence_label_for(
        {"interval_seconds": interval_seconds, "cron": cron, "run_at": run_at}
    )
    row = {
        "id": schedule_id,
        "agent_id": agent_id,
        "origin_session_id": origin_session_id,
        "name": name,
        "prompt": prompt,
        "interval_seconds": interval_seconds,
        "cron": cron,
        "timezone": tz,
        "recurrence_label": label,
        "enabled": True,
        "created_at": now,
        "last_run_at": None,
        "run_at": run_at,
    }
    await _get_db().save_schedule(
        schedule_id=schedule_id,
        agent_id=agent_id,
        name=name,
        prompt=prompt,
        created_at=now,
        interval_seconds=interval_seconds,
        cron=cron,
        timezone=tz,
        recurrence_label=label,
        origin_session_id=origin_session_id,
        run_at=run_at,
    )
    await _get_runner().add(row)
    await _broadcast_change()
    return row


def schedule_updates(existing: dict, req: UpdateScheduleRequest) -> dict:
    """The column changes a PATCH implies, recurrence included.

    Name/prompt/enabled are taken as given. Recurrence is not a column but a
    choice between three: setting one has to clear the other two, or a
    schedule that used to run every 30m and now runs at 9am would still carry
    its old interval and fire on whichever the runner reads first. So any
    recurrence field (or a bare `timezone`, which re-reads the existing cron
    in a new zone) revalidates the whole thing and writes all four columns.
    """
    fields = req.model_dump(exclude_none=True)
    updates = {k: v for k, v in fields.items() if k in ("name", "prompt", "enabled")}

    given = {
        k: fields[k]
        for k in ("cron", "interval_seconds", "run_at")
        if fields.get(k) is not None
    }
    if len(given) > 1:
        raise ScheduleParseError(
            "Pass exactly one of `cron`, `interval_seconds` or `run_at` "
            f"(got: {', '.join(sorted(given))})."
        )
    if not given and "timezone" not in fields:
        return updates

    reused = not given
    if reused:
        given = {
            k: existing.get(k)
            for k in ("cron", "interval_seconds", "run_at")
            if existing.get(k) is not None
        }
    parsed = build_explicit_schedule(
        prompt=updates.get("prompt") or existing["prompt"],
        name=updates.get("name") or existing.get("name"),
        cron=given.get("cron"),
        interval_seconds=given.get("interval_seconds"),
        run_at=given.get("run_at"),
        tz=fields.get("timezone") or existing.get("timezone"),
        # Re-reading a one-time schedule that is already due (a timezone
        # change, say) must not fail on its own stored value.
        require_future=not reused,
    )
    updates.update(
        cron=parsed.cron,
        interval_seconds=parsed.interval_seconds,
        run_at=parsed.run_at,
        timezone=parsed.timezone,
        recurrence_label=parsed.recurrence_label,
    )
    return updates


@router.get("", response_model=list[ScheduleInfo])
async def list_schedules(_: str = Depends(verify_token)):
    rows = await _get_db().load_schedules()
    return [to_schedule_info(row) for row in rows]


@router.post("", response_model=ScheduleInfo, status_code=status.HTTP_201_CREATED)
async def create_schedule(req: CreateScheduleRequest, _: str = Depends(verify_token)):
    """Create a schedule. Prefer `agent_id`; `session_id` is accepted for one
    release and resolved to the session's owning agent (agent-refactor.md
    §5.4)."""
    from ..session_manager import session_manager

    agent_id = req.agent_id
    if agent_id is None and req.session_id:
        session = session_manager.get_session(req.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        agent_id = session.agent_id
    if not agent_id:
        raise HTTPException(
            status_code=400, detail="agent_id (or legacy session_id) is required"
        )
    if await session_manager.db.get_agent(agent_id) is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    row = await create_schedule_for_agent(
        agent_id,
        req.name,
        req.prompt,
        interval_seconds=req.interval_seconds,
        origin_session_id=req.session_id,
    )
    return to_schedule_info(row)


@router.patch("/{schedule_id}", response_model=ScheduleInfo)
async def update_schedule(
    schedule_id: str, req: UpdateScheduleRequest, _: str = Depends(verify_token)
):
    db = _get_db()
    rows = await db.load_schedules()
    existing = next((r for r in rows if r["id"] == schedule_id), None)
    if not existing:
        raise HTTPException(status_code=404, detail="Schedule not found")

    try:
        updates = schedule_updates(existing, req)
    except ScheduleParseError as e:
        raise HTTPException(422, str(e))
    if not updates:
        return to_schedule_info(existing)

    await db.update_schedule(schedule_id, **updates)
    existing.update(updates)
    await _get_runner().reschedule(existing)
    await _broadcast_change()
    return to_schedule_info(existing)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(schedule_id: str, _: str = Depends(verify_token)):
    await _get_runner().remove(schedule_id)
    await _get_db().delete_schedule(schedule_id)
    await _broadcast_change()


# --------------------------------------------------------------------------- #
# Session-scoped: an agent's own schedules (schedule-tool.md §4)
# --------------------------------------------------------------------------- #


def _session_agent_id(session_id: str) -> str:
    """The agent that owns this session — the only agent these routes act for.

    Scoping is derived, never passed: the caller is an MCP shim running inside
    the turn, and the one thing it can be trusted with is which session it was
    spawned for. An agent can therefore neither read nor rewrite another
    agent's schedules through this surface.
    """
    from ..session_manager import session_manager

    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Session {session_id} not found"
        )
    if session.agent_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Session {session_id} is not bound to an agent",
        )
    return session.agent_id


async def _owned_row(schedule_id: str, agent_id: str) -> dict:
    rows = await _get_db().load_schedules()
    row = next(
        (r for r in rows if r["id"] == schedule_id and r["agent_id"] == agent_id),
        None,
    )
    if row is None:
        # Another agent's schedule is reported as missing rather than
        # forbidden: from inside this session it may as well not exist.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")
    return row


@session_router.get("/{session_id}/schedules", response_model=list[ScheduleInfo])
async def list_session_schedules(session_id: str, _: str = Depends(verify_token)):
    agent_id = _session_agent_id(session_id)
    rows = await _get_db().load_schedules()
    return [to_schedule_info(r) for r in rows if r["agent_id"] == agent_id]


@session_router.post(
    "/{session_id}/schedules",
    response_model=ScheduleInfo,
    status_code=status.HTTP_201_CREATED,
)
async def create_session_schedule(
    session_id: str, req: AgentScheduleRequest, _: str = Depends(verify_token)
):
    """Create a schedule for this session's agent, with the recurrence stated
    outright (no AI parse). `in_session` decides where the fires land: this
    conversation, or a throwaway session per fire."""
    agent_id = _session_agent_id(session_id)
    try:
        parsed = build_explicit_schedule(
            prompt=req.prompt,
            name=req.name,
            cron=req.cron,
            interval_seconds=req.interval_seconds,
            run_at=req.run_at,
            tz=req.timezone,
        )
    except ScheduleParseError as e:
        raise HTTPException(422, str(e))

    row = await create_schedule_for_agent(
        agent_id,
        parsed.name,
        parsed.prompt,
        interval_seconds=parsed.interval_seconds,
        cron=parsed.cron,
        tz=parsed.timezone,
        recurrence_label=parsed.recurrence_label,
        run_at=parsed.run_at,
        origin_session_id=session_id if req.in_session else None,
    )
    return to_schedule_info(row)


@session_router.patch(
    "/{session_id}/schedules/{schedule_id}", response_model=ScheduleInfo
)
async def update_session_schedule(
    session_id: str,
    schedule_id: str,
    req: UpdateScheduleRequest,
    _: str = Depends(verify_token),
):
    agent_id = _session_agent_id(session_id)
    existing = await _owned_row(schedule_id, agent_id)
    try:
        updates = schedule_updates(existing, req)
    except ScheduleParseError as e:
        raise HTTPException(422, str(e))
    if not updates:
        return to_schedule_info(existing)

    await _get_db().update_schedule(schedule_id, **updates)
    existing.update(updates)
    await _get_runner().reschedule(existing)
    await _broadcast_change()
    return to_schedule_info(existing)


@session_router.delete(
    "/{session_id}/schedules/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_session_schedule(
    session_id: str, schedule_id: str, _: str = Depends(verify_token)
):
    agent_id = _session_agent_id(session_id)
    await _owned_row(schedule_id, agent_id)
    await _get_runner().remove(schedule_id)
    await _get_db().delete_schedule(schedule_id)
    await _broadcast_change()
