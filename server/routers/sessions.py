from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth import verify_token
from ..models import CreateSessionRequest, ImportSessionRequest, MessageContent, PendingQuestionInfo, SessionDetail, SessionInfo, SessionStatus
from ..session_manager import session_manager

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _to_session_info(
    s, message_count: int | None = None, archived: bool = False
) -> SessionInfo:
    return SessionInfo(
        id=s.id,
        name=s.name,
        working_dir=s.working_dir,
        status=s.status,
        created_at=s.created_at,
        message_count=s._message_count if message_count is None else message_count,
        claude_session_id=s.claude_session_id,
        credential_id=s.credential_id,
        agent_id=s.agent_id,
        origin=s.origin,
        backend=s.backend,
        archived=archived,
    )


@router.get("", response_model=list[SessionInfo])
async def list_sessions(
    include_archived: bool = Query(False),
    _: str = Depends(verify_token),
):
    live = [_to_session_info(s) for s in session_manager.list_sessions()]
    if not include_archived:
        return live
    archived = await session_manager.list_archived_sessions()
    return live + archived


async def _check_credential_backend(credential_id: str | None, backend: str) -> None:
    """A session must not run a credential whose backend differs from its own
    (codex-backend.md §4.2) — e.g. a Codex subscription on a Claude session.
    400 on mismatch. A missing credential is tolerated (resolved later)."""
    if not credential_id:
        return
    row = await session_manager.db.get_credential(credential_id)
    if row is None:
        return
    if row["backend"] != backend:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Credential backend {row['backend']!r} does not match session "
            f"backend {backend!r}",
        )


@router.post("", response_model=SessionInfo, status_code=status.HTTP_201_CREATED)
async def create_session(
    req: CreateSessionRequest, _: str = Depends(verify_token)
):
    # A session is owned by an agent. agent_id is required, but for exactly
    # one release we fall back to the Default Agent when the client omits it
    # (agent-refactor.md §5.4).
    agent_id = req.agent_id
    if agent_id is None:
        sys_agent = await session_manager.db.get_system_agent()
        agent_id = sys_agent["id"] if sys_agent else None
    # Inherit the owning agent's default backend when none is pinned.
    agent = await session_manager.db.get_agent(agent_id) if agent_id else None
    backend = (
        req.backend.value
        if req.backend is not None
        else (agent.get("backend") if agent else None) or "claude-code"
    )
    await _check_credential_backend(req.credential_id, backend)
    try:
        s = await session_manager.create_session(
            agent_id,
            req.name,
            req.working_dir,
            credential_id=req.credential_id,
            backend=backend,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return _to_session_info(s, message_count=0)


@router.post("/import", response_model=SessionDetail, status_code=status.HTTP_201_CREATED)
async def import_session(
    req: ImportSessionRequest, _: str = Depends(verify_token)
):
    agent_id = req.agent_id
    if agent_id is None:
        sys_agent = await session_manager.db.get_system_agent()
        agent_id = sys_agent["id"] if sys_agent else None
    s = await session_manager.import_session(
        name=req.name,
        working_dir=req.working_dir,
        claude_session_id=req.claude_session_id,
        credential_id=req.credential_id,
        messages=req.messages,
        agent_id=agent_id,
        backend=req.backend.value,
    )
    messages_raw = await session_manager.db.load_messages(s.id)
    messages = [MessageContent(**m) for m in messages_raw]
    return SessionDetail(
        id=s.id,
        name=s.name,
        working_dir=s.working_dir,
        status=s.status,
        created_at=s.created_at,
        message_count=s._message_count,
        claude_session_id=s.claude_session_id,
        credential_id=s.credential_id,
        agent_id=s.agent_id,
        origin=s.origin,
        backend=s.backend,
        messages=messages,
        pending_queue=[qp.prompt for qp in s._pending_queue],
        pending_questions=[
            PendingQuestionInfo(question_id=q.question_id, questions=q.questions)
            for q in s._pending_questions.values()
        ],
        # High-water mark: clients use this as the dedup baseline so any
        # WS event with seq < next_message_seq is treated as already
        # applied (it's in the messages list above).
        next_message_seq=s._message_count,
    )


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session(session_id: str, _: str = Depends(verify_token)):
    # Live session: read straight from the in-memory state (includes
    # pending queue / pending questions / live status).
    s = session_manager.get_session(session_id)
    if s is not None:
        messages_raw = await session_manager.db.load_messages(s.id)
        messages = [MessageContent(**m) for m in messages_raw]
        return SessionDetail(
            id=s.id,
            name=s.name,
            working_dir=s.working_dir,
            status=s.status,
            created_at=s.created_at,
            message_count=s._message_count,
            claude_session_id=s.claude_session_id,
            credential_id=s.credential_id,
            agent_id=s.agent_id,
            origin=s.origin,
            backend=s.backend,
            messages=messages,
            pending_queue=[qp.prompt for qp in s._pending_queue],
            pending_questions=[
                PendingQuestionInfo(question_id=q.question_id, questions=q.questions)
                for q in s._pending_questions.values()
            ],
            # High-water mark: clients use this as the dedup baseline so any
            # WS event with seq < next_message_seq is treated as already
            # applied (it's in the messages list above).
            next_message_seq=s._message_count,
        )

    # Archived session: not in memory; read history straight from DB.
    archived_detail = await session_manager.load_archived_session_detail(session_id)
    if archived_detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return archived_detail


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: str, _: str = Depends(verify_token)):
    deleted = await session_manager.delete_session(session_id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")


@router.post("/{session_id}/reset")
async def reset_session(session_id: str, _: str = Depends(verify_token)):
    try:
        await session_manager.reset_session(session_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return {"status": "ok"}


@router.post(
    "/{session_id}/archive",
    response_model=SessionInfo,
    status_code=status.HTTP_201_CREATED,
)
async def archive_session(session_id: str, _: str = Depends(verify_token)):
    """Archive the current session and return a fresh one.

    Same name / working_dir / credential_id as the archived session,
    but a brand-new id and no message history. Schedules + bridge
    mappings repoint from old to new so user-facing automation
    continues uninterrupted.
    """
    try:
        new = await session_manager.archive_session(session_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return _to_session_info(new)


@router.post("/{session_id}/unarchive", response_model=SessionInfo)
async def unarchive_session(session_id: str, _: str = Depends(verify_token)):
    """Bring an archived session back as a live session.

    Flips the DB row's `archived=0` and reloads it into the in-memory
    session map so writes (sendMessage, schedules, etc.) work again.
    """
    try:
        s = await session_manager.unarchive_session(session_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return _to_session_info(s)
