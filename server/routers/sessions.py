from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import verify_token
from ..models import CreateSessionRequest, ImportSessionRequest, SessionDetail, SessionInfo, SessionStatus
from ..session_manager import session_manager

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("", response_model=list[SessionInfo])
async def list_sessions(_: str = Depends(verify_token)):
    sessions = session_manager.list_sessions()
    return [
        SessionInfo(
            id=s.id,
            name=s.name,
            working_dir=s.working_dir,
            status=s.status,
            created_at=s.created_at,
            message_count=len(s.messages),
        )
        for s in sessions
    ]


@router.post("", response_model=SessionInfo, status_code=status.HTTP_201_CREATED)
async def create_session(
    req: CreateSessionRequest, _: str = Depends(verify_token)
):
    s = await session_manager.create_session(req.name, req.working_dir)
    return SessionInfo(
        id=s.id,
        name=s.name,
        working_dir=s.working_dir,
        status=s.status,
        created_at=s.created_at,
        message_count=0,
    )


@router.post("/import", response_model=SessionDetail, status_code=status.HTTP_201_CREATED)
async def import_session(
    req: ImportSessionRequest, _: str = Depends(verify_token)
):
    s = await session_manager.import_session(
        name=req.name,
        working_dir=req.working_dir,
        claude_session_id=req.claude_session_id,
        messages=req.messages,
    )
    return SessionDetail(
        id=s.id,
        name=s.name,
        working_dir=s.working_dir,
        status=s.status,
        created_at=s.created_at,
        message_count=len(s.messages),
        messages=s.messages,
    )


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session(session_id: str, _: str = Depends(verify_token)):
    s = session_manager.get_session(session_id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return SessionDetail(
        id=s.id,
        name=s.name,
        working_dir=s.working_dir,
        status=s.status,
        created_at=s.created_at,
        message_count=len(s.messages),
        messages=s.messages,
    )


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: str, _: str = Depends(verify_token)):
    deleted = await session_manager.delete_session(session_id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
