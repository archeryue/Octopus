from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import verify_token
from ..models import CreateScheduleRequest, ScheduleInfo, UpdateScheduleRequest

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


@router.get("", response_model=list[ScheduleInfo])
async def list_schedules(_: str = Depends(verify_token)):
    rows = await _get_db().load_schedules()
    return [ScheduleInfo(**row) for row in rows]


@router.post("", response_model=ScheduleInfo, status_code=status.HTTP_201_CREATED)
async def create_schedule(req: CreateScheduleRequest, _: str = Depends(verify_token)):
    from ..session_manager import session_manager

    if not session_manager.sessions.get(req.session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    schedule_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "id": schedule_id,
        "session_id": req.session_id,
        "name": req.name,
        "prompt": req.prompt,
        "interval_seconds": req.interval_seconds,
        "enabled": True,
        "created_at": now,
        "last_run_at": None,
    }
    await _get_db().save_schedule(
        schedule_id=schedule_id,
        session_id=req.session_id,
        name=req.name,
        prompt=req.prompt,
        interval_seconds=req.interval_seconds,
        created_at=now,
    )
    await _get_runner().add(row)
    return ScheduleInfo(**row)


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
        return ScheduleInfo(**existing)

    await db.update_schedule(schedule_id, **updates)
    existing.update(updates)
    await _get_runner().reschedule(existing)
    return ScheduleInfo(**existing)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(schedule_id: str, _: str = Depends(verify_token)):
    await _get_runner().remove(schedule_id)
    await _get_db().delete_schedule(schedule_id)
