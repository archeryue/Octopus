"""REST endpoints for cross-turn background tasks.

The frontend uses these to render the chip + popover (list/get
endpoints) and to wire up the cancel button. The MCP server in
`server/mcp_servers/bg.py` uses the POST endpoint to start tasks —
it's a child of the `claude` CLI and can't reach the in-process
manager any other way.

Auth: same bearer token as every other API surface.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..auth import verify_token
from ..bg_tasks import BgTaskError, BgTaskRecord, bg_task_manager
from ..session_manager import session_manager

router = APIRouter(prefix="/api/sessions", tags=["bg-tasks"])


class StartBgTaskRequest(BaseModel):
    command: str
    description: str | None = None


def _record_to_json(rec: BgTaskRecord) -> dict[str, Any]:
    return {
        "id": rec.id,
        "session_id": rec.session_id,
        "command": rec.command,
        "description": rec.description,
        "working_dir": rec.working_dir,
        "status": rec.status,
        "exit_code": rec.exit_code,
        "stdout": rec.stdout,
        "stderr": rec.stderr,
        "truncated": rec.truncated,
        "started_at": rec.started_at,
        "completed_at": rec.completed_at,
    }


def _require_session(session_id: str) -> str:
    """Live sessions only — bg tasks attach to in-memory sessions so
    the cross-turn delivery has a target. Archived sessions are
    read-only history."""
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Session {session_id} not found"
        )
    return session.working_dir


@router.post(
    "/{session_id}/bg-tasks",
    status_code=status.HTTP_201_CREATED,
)
async def start_bg_task(
    session_id: str,
    req: StartBgTaskRequest,
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    """Start a new background task. Called by the bg MCP server.

    Returns the freshly-created task row so the MCP tool can echo the
    task_id back to the model. The subprocess is already running by the
    time this returns — callers don't wait for completion.
    """
    if not req.command.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "command must be non-empty")
    working_dir = _require_session(session_id)
    try:
        rec = await bg_task_manager.start_task(
            session_id=session_id,
            command=req.command,
            working_dir=working_dir,
            description=req.description,
        )
    except BgTaskError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return _record_to_json(rec)


@router.get("/{session_id}/bg-tasks")
async def list_bg_tasks(
    session_id: str,
    _: str = Depends(verify_token),
) -> list[dict[str, Any]]:
    """All bg tasks for a session, most-recent first. Used by the
    sidebar / chip popover. Includes finished tasks so users can scroll
    back through history."""
    # We don't require a live session here — chat history can outlive
    # the in-memory session (archived). The DB row is enough.
    rows = await bg_task_manager.list_tasks(session_id)
    return [_record_to_json(r) for r in rows]


@router.get("/{session_id}/bg-tasks/{task_id}")
async def get_bg_task(
    session_id: str,
    task_id: str,
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    rec = await bg_task_manager.get_task(task_id)
    if rec is None or rec.session_id != session_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Background task not found")
    return _record_to_json(rec)


@router.post("/{session_id}/bg-tasks/{task_id}/cancel")
async def cancel_bg_task(
    session_id: str,
    task_id: str,
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    """Best-effort cancel. Returns {cancelled: bool}; cancelled=False
    means the task wasn't currently running (already finished, or
    server restarted and lost the in-memory handle)."""
    rec = await bg_task_manager.get_task(task_id)
    if rec is None or rec.session_id != session_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Background task not found")
    cancelled = await bg_task_manager.cancel_task(task_id)
    return {"cancelled": cancelled, "task_id": task_id}
