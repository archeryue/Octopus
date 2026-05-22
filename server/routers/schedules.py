from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import verify_token
from ..models import CreateScheduleRequest, ScheduleInfo, UpdateScheduleRequest
from ..schedule_ai import recurrence_label_for

router = APIRouter(prefix="/api/schedules", tags=["schedules"])

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
    human-readable recurrence label (derived for legacy rows that predate it)."""
    return ScheduleInfo(**{**row, "recurrence_label": recurrence_label_for(row)})


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
) -> dict:
    """Persist a schedule owned by `agent_id` and register its job. Recurrence
    is exactly one of `interval_seconds` or `cron` (+`timezone`). Shared by the
    standalone `/api/schedules` route, the agent-scoped
    `/api/agents/{id}/schedules` route, and the natural-language `from_text`
    route. `origin_session_id` (the session the command was issued from) makes
    each fire append into that conversation instead of a throwaway session."""
    schedule_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()
    label = recurrence_label or recurrence_label_for(
        {"interval_seconds": interval_seconds, "cron": cron}
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
    )
    await _get_runner().add(row)
    return row


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

    updates = req.model_dump(exclude_none=True)
    if not updates:
        return to_schedule_info(existing)

    await db.update_schedule(schedule_id, **updates)
    existing.update(updates)
    await _get_runner().reschedule(existing)
    return to_schedule_info(existing)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(schedule_id: str, _: str = Depends(verify_token)):
    await _get_runner().remove(schedule_id)
    await _get_db().delete_schedule(schedule_id)
