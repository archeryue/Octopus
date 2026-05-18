"""REST endpoints for async notification targets."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import verify_token
from ..models import (
    CreateNotifierRequest,
    NotifierInfo,
    UpdateNotifierRequest,
)
from ..notifiers import notifier_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notifiers", tags=["notifiers"])

_db = None


def set_db(db) -> None:
    global _db
    _db = db


def _require_db():
    if _db is None:
        raise HTTPException(
            status_code=503, detail="notifier database not yet initialized"
        )
    return _db


def _row_to_info(row: dict) -> NotifierInfo:
    return NotifierInfo(
        id=row["id"],
        type=row["type"],
        label=row["label"],
        config=row.get("config", {}),
        enabled=bool(row.get("enabled", True)),
        created_at=row["created_at"],
    )


@router.get("", response_model=list[NotifierInfo])
async def list_notifiers(_: str = Depends(verify_token)):
    rows = await _require_db().load_notifiers()
    return [_row_to_info(r) for r in rows]


@router.post(
    "", response_model=NotifierInfo, status_code=status.HTTP_201_CREATED
)
async def create_notifier(
    req: CreateNotifierRequest, _: str = Depends(verify_token)
):
    db = _require_db()
    nid = uuid.uuid4().hex[:12]
    created_at = datetime.now(timezone.utc).isoformat()
    await db.save_notifier(
        notifier_id=nid,
        type=req.type.value,
        label=req.label,
        config=req.config,
        created_at=created_at,
    )
    # Reload manager so the new target is live without restart
    await notifier_manager.load()
    return NotifierInfo(
        id=nid,
        type=req.type,
        label=req.label,
        config=req.config,
        enabled=True,
        created_at=created_at,
    )


@router.patch("/{notifier_id}", response_model=NotifierInfo)
async def update_notifier(
    notifier_id: str,
    req: UpdateNotifierRequest,
    _: str = Depends(verify_token),
):
    db = _require_db()
    rows = await db.load_notifiers()
    existing = next((r for r in rows if r["id"] == notifier_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail="notifier not found")
    updates: dict = {}
    if req.label is not None:
        updates["label"] = req.label
    if req.config is not None:
        updates["config"] = req.config
    if req.enabled is not None:
        updates["enabled"] = req.enabled
    if updates:
        await db.update_notifier(notifier_id, **updates)
        await notifier_manager.load()
    rows = await db.load_notifiers()
    updated = next((r for r in rows if r["id"] == notifier_id), None)
    assert updated is not None
    return _row_to_info(updated)


@router.delete("/{notifier_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notifier(notifier_id: str, _: str = Depends(verify_token)):
    db = _require_db()
    deleted = await db.delete_notifier(notifier_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="notifier not found")
    await notifier_manager.load()
