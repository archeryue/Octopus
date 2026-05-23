from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..agent_manager import AgentError, AgentManager
from ..auth import verify_token
from ..models import (
    AgentCreate,
    AgentRead,
    AgentUpdate,
    CreateScheduleRequest,
    CreateSessionRequest,
    ScheduleFromTextRequest,
    ScheduleInfo,
    SessionInfo,
)
from ..session_manager import session_manager

router = APIRouter(prefix="/api/agents", tags=["agents"])

# Injected at startup (mirrors schedules/credentials routers).
_manager: AgentManager | None = None


def set_manager(mgr: AgentManager) -> None:
    global _manager
    _manager = mgr


def _get_manager() -> AgentManager:
    assert _manager is not None
    return _manager


def _agent_http_error(e: AgentError) -> HTTPException:
    msg = str(e)
    code = status.HTTP_404_NOT_FOUND if "not found" in msg.lower() else status.HTTP_400_BAD_REQUEST
    return HTTPException(code, msg)


@router.get("", response_model=list[AgentRead])
async def list_agents(
    include_archived: bool = Query(False), _: str = Depends(verify_token)
):
    agents = await _get_manager().list_agents(include_archived=include_archived)
    return [AgentRead(**a) for a in agents]


@router.post("", response_model=AgentRead, status_code=status.HTTP_201_CREATED)
async def create_agent(req: AgentCreate, _: str = Depends(verify_token)):
    try:
        agent = await _get_manager().create_agent(**req.model_dump())
    except AgentError as e:
        raise _agent_http_error(e)
    return AgentRead(**agent)


@router.get("/{agent_id}", response_model=AgentRead)
async def get_agent(agent_id: str, _: str = Depends(verify_token)):
    agent = await _get_manager().get_agent(agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    return AgentRead(**agent)


@router.patch("/{agent_id}", response_model=AgentRead)
async def update_agent(
    agent_id: str, req: AgentUpdate, _: str = Depends(verify_token)
):
    # exclude_unset so omitting a field leaves it untouched while explicitly
    # passing null clears a nullable field (model/credential_id/avatar).
    fields = req.model_dump(exclude_unset=True)
    try:
        agent = await _get_manager().update_agent(agent_id, **fields)
    except AgentError as e:
        raise _agent_http_error(e)
    return AgentRead(**agent)


@router.post("/{agent_id}/archive", response_model=AgentRead)
async def archive_agent(agent_id: str, _: str = Depends(verify_token)):
    try:
        await _get_manager().archive_agent(agent_id)
    except AgentError as e:
        raise _agent_http_error(e)
    # DB rows are archived by the manager; evict the agent's sessions from
    # the in-memory map so they leave the live list immediately.
    await session_manager.evict_agent_sessions(agent_id)
    agent = await _get_manager().get_agent(agent_id)
    return AgentRead(**agent)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(agent_id: str, _: str = Depends(verify_token)):
    try:
        await _get_manager().delete_agent(agent_id)
    except AgentError as e:
        raise _agent_http_error(e)


@router.get("/{agent_id}/sessions", response_model=list[SessionInfo])
async def list_agent_sessions(agent_id: str, _: str = Depends(verify_token)):
    if await _get_manager().get_agent(agent_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    from .sessions import _to_session_info

    return [
        _to_session_info(s)
        for s in session_manager.list_sessions()
        if s.agent_id == agent_id
    ]


@router.post(
    "/{agent_id}/sessions",
    response_model=SessionInfo,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_session(
    agent_id: str, req: CreateSessionRequest, _: str = Depends(verify_token)
):
    """Preferred path to start a session — the agent comes from the URL, so
    the body's `agent_id` (if any) is ignored."""
    from .sessions import _check_credential_backend, _to_session_info

    # Inherit the agent's default backend when the request doesn't pin one.
    agent = await _get_manager().get_agent(agent_id)
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


@router.get("/{agent_id}/schedules", response_model=list[ScheduleInfo])
async def list_agent_schedules(agent_id: str, _: str = Depends(verify_token)):
    if await _get_manager().get_agent(agent_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    from .schedules import _get_db, to_schedule_info

    rows = await _get_db().load_schedules()
    return [to_schedule_info(r) for r in rows if r["agent_id"] == agent_id]


@router.post(
    "/{agent_id}/schedules",
    response_model=ScheduleInfo,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_schedule(
    agent_id: str, req: CreateScheduleRequest, _: str = Depends(verify_token)
):
    if await _get_manager().get_agent(agent_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    from .schedules import create_schedule_for_agent, to_schedule_info

    row = await create_schedule_for_agent(
        agent_id,
        req.name,
        req.prompt,
        interval_seconds=req.interval_seconds,
        origin_session_id=req.session_id,
    )
    return to_schedule_info(row)


@router.post(
    "/{agent_id}/schedules/from_text",
    response_model=ScheduleInfo,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_schedule_from_text(
    agent_id: str, req: ScheduleFromTextRequest, _: str = Depends(verify_token)
):
    """Natural-language schedule creation. Parses `text` (rigid `<interval>
    <prompt>` fast-path, else a one-shot AI parse on the agent's own harness)
    into a recurrence + prompt, then creates the schedule. Parse failures
    surface as 422 with a user-facing detail."""
    agent = await _get_manager().get_agent(agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    from .schedules import create_schedule_for_agent, to_schedule_info
    from ..harness import get_harness
    from ..schedule_ai import ScheduleParseError, parse_schedule_text

    # The AI parse runs on the agent's own harness (claude-code or codex —
    # both support one-shot, D2), with its own model + credential resolved in
    # the shape that harness needs. No backend-kind branching.
    harness = get_harness(agent.get("backend"))
    credential = await session_manager.resolve_credential_by_id(
        agent.get("credential_id"),
        style=harness.profile.credential_style,
        context=f"schedule-parse agent {agent_id}",
    )
    try:
        parsed = await parse_schedule_text(
            req.text,
            harness=harness,
            timezone=req.timezone,
            now_iso=req.now,
            model=agent.get("model") or None,
            credential=credential,
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
        origin_session_id=req.session_id,
    )
    return to_schedule_info(row)
