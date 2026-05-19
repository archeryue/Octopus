"""REST endpoints for the MCP-based AskUserQuestion flow.

Three endpoints:

  POST /api/sessions/{id}/questions
      Called by the `ask` MCP server (server/mcp_servers/ask.py) when
      the model invokes `mcp__ask__user`. Body: `{questions: [...]}`.
      Octopus generates a question_id, persists the question into the
      session's chat history (so reload re-renders the form), and
      broadcasts the `question_request` WS event to the frontend.
      Returns `{question_id: "..."}`.

  GET  /api/sessions/{id}/questions/{question_id}/answer
      Long-polled by the ask MCP server. Blocks on an asyncio.Event
      until the user submits the answer (or the per-call timeout
      hits). Returns `{answer: "..."}` on success, or 408 on timeout
      so the MCP server can retry. The MCP server retries until it
      gets an answer or the session-level auto-answer timer fires.

  POST /api/sessions/{id}/questions/{question_id}/answer
      Called by the frontend when the user submits the form. Routes
      into session_manager.answer_question() which sets the Event
      that unblocks the long-poll, persists the answer, and
      broadcasts the question_answer WS event.

Auth: same bearer-token model as every other API surface. The MCP
server holds OCTOPUS_AUTH_TOKEN; the frontend sends the user's
bearer.

This replaces the previous CLI control-protocol path
(`--permission-prompt-tool=stdio` + deny-channel answer) that was
exposing us to the CLI's premature-exit bug at large context scale.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..auth import verify_token
from ..session_manager import session_manager

router = APIRouter(prefix="/api/sessions", tags=["questions"])


class CreateQuestionRequest(BaseModel):
    """Body for POST /questions. The schema mirrors the existing
    AskUserQuestion tool input so the model's call shape is unchanged."""
    questions: list[dict[str, Any]]


class AnswerItem(BaseModel):
    """One entry in the frontend's answer submission. Mirrors the
    legacy WS answer_question payload so existing UI code keeps
    working."""
    selected: list[str] | None = None
    text: str | None = None


class SubmitAnswerRequest(BaseModel):
    answers: list[AnswerItem]


@router.post(
    "/{session_id}/questions",
    status_code=status.HTTP_201_CREATED,
)
async def create_question(
    session_id: str,
    req: CreateQuestionRequest,
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    """Called by the MCP server. Creates a pending question, broadcasts
    the WS event so the frontend renders the form, returns the
    question_id the MCP server should long-poll on."""
    if not req.questions:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "questions must be non-empty")
    if session_manager.get_session(session_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    qid = await session_manager.create_pending_question(session_id, req.questions)
    if qid is None:
        # The session existed at check time but vanished — race with
        # delete. Surface as 404 so the MCP server can report cleanly.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return {"question_id": qid}


@router.get("/{session_id}/questions/{question_id}/answer")
async def wait_for_answer(
    session_id: str,
    question_id: str,
    timeout: float = 60.0,
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    """MCP-server-facing long-poll. Returns when the user (or the
    session-level auto-answer timeout) submits. 408 on per-call
    timeout — the MCP server should loop and retry."""
    if session_manager.get_session(session_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    # Cap timeout so a misbehaving MCP server can't park a connection
    # forever. 5 min is plenty given the auto-answer default is 30 min.
    timeout = min(max(timeout, 1.0), 300.0)
    answer = await session_manager.wait_for_question_answer(
        session_id, question_id, timeout=timeout
    )
    if answer is None:
        # Distinguishable status code so the MCP server loops vs. errors.
        raise HTTPException(status.HTTP_408_REQUEST_TIMEOUT, "no answer yet")
    return {"answer": answer}


@router.post("/{session_id}/questions/{question_id}/answer")
async def submit_answer(
    session_id: str,
    question_id: str,
    req: SubmitAnswerRequest,
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    """Frontend-facing. The user pressed submit; route into the
    session_manager which formats the answer, persists the chat
    entry, broadcasts the question_answer WS event, and sets the
    Event that wakes the MCP server's long-poll."""
    if session_manager.get_session(session_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    ok = await session_manager.answer_question(
        session_id,
        question_id,
        [a.model_dump() for a in req.answers],
    )
    if not ok:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Question not found or already answered",
        )
    return {"ok": True}
