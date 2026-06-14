"""REST endpoints for native deep research (native-deep-research.md §7).

Thin translation layer between HTTP and the in-process `ResearchManager`,
mirroring the delegations/bg routers. Session-scoped
(`/api/sessions/{sid}/research`) because a job always belongs to a session.
The `mcp__research__deep_research` MCP server POSTs here from inside a turn;
the `/research` slash command and the UI use the same routes. Bearer auth.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import verify_token
from ..research import ResearchError, research_manager

router = APIRouter(prefix="/api/sessions", tags=["research"])


class StartResearchRequest(BaseModel):
    question: str = Field(min_length=1)


@router.post("/{session_id}/research", status_code=201)
async def start_research(
    session_id: str, req: StartResearchRequest, _: str = Depends(verify_token)
) -> dict[str, Any]:
    """Launch a deep-research job. Returns immediately with the job row; the
    final report is injected into the session as a turn when it finishes."""
    try:
        return await research_manager.start(session_id, req.question)
    except ResearchError as e:
        raise HTTPException(e.status_code, e.message)


@router.get("/{session_id}/research")
async def list_research(
    session_id: str, _: str = Depends(verify_token)
) -> list[dict[str, Any]]:
    if research_manager.db is None:
        return []
    return await research_manager.db.list_research_jobs_for_session(session_id)


@router.get("/{session_id}/research/{research_id}")
async def get_research(
    session_id: str, research_id: str, _: str = Depends(verify_token)
) -> dict[str, Any]:
    if research_manager.db is None:
        raise HTTPException(503, "research not available")
    row = await research_manager.db.get_research_job(research_id)
    if row is None or row["session_id"] != session_id:
        raise HTTPException(404, "research job not found")
    return row


@router.post("/{session_id}/research/{research_id}/cancel")
async def cancel_research(
    session_id: str, research_id: str, _: str = Depends(verify_token)
) -> dict[str, Any]:
    # Verify session ownership BEFORE mutating — a request scoped to the wrong
    # session must not cancel a real job (Vera review).
    if research_manager.db is None:
        raise HTTPException(503, "research not available")
    existing = await research_manager.db.get_research_job(research_id)
    if existing is None or existing["session_id"] != session_id:
        raise HTTPException(404, "research job not found")
    try:
        return await research_manager.cancel(research_id)
    except ResearchError as e:
        raise HTTPException(e.status_code, e.message)
