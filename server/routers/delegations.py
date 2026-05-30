"""REST endpoints for agent-to-agent delegations (agent-collaboration.md).

Shape mirrors the bg-tasks router: a thin translation layer between
HTTP and the in-process `DelegationManager`. The `ask_agent` MCP server
(Phase 2) will POST to these endpoints from inside a running harness;
in Phase 1 the only caller is tests, by design.

All routes are session-scoped (`/api/sessions/{sid}/delegations`)
because a delegation always has a parent session. The delegation id
*is* the child session id — there's no parallel id space.

Auth: same bearer token as every other API surface.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..auth import verify_token
from ..delegations import DelegationError, delegation_manager
from ..session_manager import session_manager

router = APIRouter(prefix="/api/sessions", tags=["delegations"])


class StartDelegationRequest(BaseModel):
    agent_name: str
    request: str
    # Optional list of file paths the parent agent thinks the child
    # should look at. Rendered into the child's first user message
    # verbatim; we don't validate existence here (the child's tools
    # will report missing files naturally).
    files: list[str] | None = None


class CancelDelegationRequest(BaseModel):
    reason: str | None = None


def _require_session(session_id: str) -> None:
    """Live sessions only — delegations attach to in-memory sessions so
    the broadcast listener has a target. Archived sessions are
    read-only history."""
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Session {session_id} not found"
        )


@router.post(
    "/{session_id}/delegations",
    status_code=status.HTTP_201_CREATED,
)
async def start_delegation(
    session_id: str,
    req: StartDelegationRequest,
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    """Spawn a child session under the named agent and start its first
    turn. Returns immediately — the reply arrives later as a turn
    injected into the parent session.

    Status codes:
    - 201 Created with the delegation record on success
    - 404 if the parent session is gone or the target agent name
      doesn't resolve
    - 409 on cycle, depth, self-delegation, or ambiguous name
    """
    _require_session(session_id)
    try:
        rec = await delegation_manager.start_delegation(
            parent_session_id=session_id,
            agent_name=req.agent_name,
            request=req.request,
            files=req.files,
        )
    except DelegationError as e:
        raise HTTPException(e.status_code, str(e))
    return rec.to_public_dict()


@router.get("/{session_id}/delegations")
async def list_delegations(
    session_id: str,
    _: str = Depends(verify_token),
) -> list[dict[str, Any]]:
    """Recent delegations spawned by this session, newest first. The
    list includes finished ones so the model can see what it's
    asked recently. Currently capped at 25 inside the manager."""
    # No `_require_session` here: a parent session may have been
    # archived while a delegation is still in our in-memory registry;
    # we still want list to work for inspection in that case.
    rows = delegation_manager.list_delegations(session_id)
    return [r.to_public_dict() for r in rows]


@router.post("/{session_id}/delegations/{delegation_id}/cancel")
async def cancel_delegation(
    session_id: str,
    delegation_id: str,
    req: CancelDelegationRequest | None = None,
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    """Best-effort cancel. Idempotent — cancelling a finished
    delegation returns the existing terminal record without touching
    anything. The parent gets an `[agent-error:…]` injection on the
    transition from running → cancelled."""
    rec = delegation_manager.get_delegation(delegation_id)
    if rec is None or rec.parent_session_id != session_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Delegation not found"
        )
    try:
        updated = await delegation_manager.cancel_delegation(
            delegation_id, reason=(req.reason if req else None)
        )
    except DelegationError as e:
        raise HTTPException(e.status_code, str(e))
    return updated.to_public_dict()
