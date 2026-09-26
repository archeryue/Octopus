"""Questions the model asks the human, and tool-approval decisions.

One slice of `SessionManager`, which was a single 4,017-line class with 84
methods (docs/plans/polish-2026-09.md §3 A1). Split by responsibility; the
methods are unchanged and still reached through one `session_manager` object,
so no call site moved.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from ..config import settings
from ..models import MessageContent, MessageRole
from .base import (
    PendingQuestion,
    Session,
    SessionManagerBase,
    logger,
)


class QuestionsMixin(SessionManagerBase):

    async def create_pending_question(
        self,
        session_id: str,
        questions: list[dict[str, Any]],
    ) -> str | None:
        """Called by the ask MCP server (via REST) when the model invokes
        `mcp__ask__user`. Generates a question_id, records the pending
        question, broadcasts the `question_request` WS event so the
        frontend renders the form, and schedules the auto-answer
        timeout. Returns the question_id (which the MCP server then
        passes to the long-poll endpoint).
        """
        session = self.sessions.get(session_id)
        if session is None:
            return None
        question_id = uuid.uuid4().hex[:16]
        session._pending_questions[question_id] = PendingQuestion(
            question_id=question_id,
            questions=questions,
        )
        session._pending_question_events[question_id] = asyncio.Event()

        # Persist + broadcast a `question_request` matching the shape the
        # frontend already expects. The persisted MessageContent makes
        # the question visible in chat history on reconnect.
        msg = MessageContent(
            role=MessageRole.assistant,
            type="question_request",
            tool_name="AskUserQuestion",
            tool_use_id=question_id,
            tool_input={"questions": questions},
        )
        msg_seq = await self._persist_message(session, msg)
        event: dict[str, Any] = {
            "type": "question_request",
            "session_id": session.id,
            "question_id": question_id,
            "questions": questions,
        }
        if msg_seq is not None:
            event["seq"] = msg_seq
        await self._broadcast(event)
        self._schedule_question_timeout(session, question_id)
        return question_id

    async def wait_for_question_answer(
        self,
        session_id: str,
        question_id: str,
        timeout: float = 60.0,
    ) -> str | None:
        """Long-poll waiter used by the ask MCP server's HTTP loop.

        Returns the answer text when the user (or auto-answer) submits,
        None on timeout. The MCP server retries on None until it gets
        an answer or hits its own outer limit.
        """
        session = self.sessions.get(session_id)
        if session is None:
            return None
        ev = session._pending_question_events.get(question_id)
        if ev is None:
            # Already-delivered case: answer might be sitting in the
            # answers dict from a fast delivery; return immediately.
            ans = session._pending_question_answers.get(question_id)
            return ans
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except TimeoutError:
            return None
        return session._pending_question_answers.get(question_id)

    async def answer_question(
        self,
        session_id: str,
        question_id: str,
        answers: list[dict[str, Any]],
    ) -> bool:
        """Called by the frontend (REST or legacy WS) when the user
        submits answers to the form. Formats them, stores the text,
        wakes any waiting MCP-server long-poll via the asyncio.Event,
        persists the chat history entry, and broadcasts the WS event.
        """
        session = self.sessions.get(session_id)
        if not session:
            return False
        pending = session._pending_questions.get(question_id)
        if pending is None:
            return False
        answer_text = self._format_answers(pending.questions, answers)
        return await self._deliver_question_answer(
            session, question_id, answer_text, auto=False
        )

    async def _deliver_question_answer(
        self,
        session: Session,
        question_id: str,
        answer_text: str,
        *,
        auto: bool,
    ) -> bool:
        """Common path for both human and timeout-driven answers.

        Sets the per-question Event so the ask MCP server's long-poll
        unblocks and returns the answer to the model. Persists the
        user-visible question_answer chat entry, broadcasts the WS
        event with the `auto` flag set when the timeout fired.
        """
        self._cancel_question_timer(session, question_id)

        # Stash the text + signal the waiter. Even if no MCP long-poll
        # is currently waiting (e.g. the MCP request retried just now),
        # the answer sits in the answers dict for the next poll.
        session._pending_question_answers[question_id] = answer_text
        ev = session._pending_question_events.get(question_id)
        if ev is not None:
            ev.set()

        ans_msg = MessageContent(
            role=MessageRole.user,
            type="question_answer",
            tool_use_id=question_id,
            content=answer_text,
        )
        ans_seq = await self._persist_message(session, ans_msg)
        event: dict[str, Any] = {
            "type": "question_answer",
            "session_id": session.id,
            "question_id": question_id,
            "content": answer_text,
        }
        if auto:
            event["auto"] = True
        if ans_seq is not None:
            event["seq"] = ans_seq
        await self._broadcast(event)

        # Keep the answer text around briefly for any in-flight MCP
        # long-poll that arrives just AFTER set() — it'll fetch from
        # the answers dict directly. We clean up at session reset /
        # delete / archive instead of immediately, since a stale
        # answer dict entry is cheap.
        session._pending_questions.pop(question_id, None)
        return True

    def _schedule_question_timeout(self, session: Session, question_id: str) -> None:
        timeout = settings.ask_user_question_timeout_seconds
        if timeout <= 0:
            return  # auto-answer disabled
        # Replace any existing timer for this question_id — defensive,
        # we don't expect the same id to be emitted twice.
        self._cancel_question_timer(session, question_id)
        task = asyncio.create_task(
            self._auto_answer_after(session, question_id, timeout),
            name=f"auto-answer-{session.id}-{question_id}",
        )
        session._question_timers[question_id] = task

    def _cancel_question_timer(self, session: Session, question_id: str) -> None:
        task = session._question_timers.pop(question_id, None)
        if task and not task.done():
            task.cancel()

    def _cancel_all_question_timers(self, session: Session) -> None:
        for task in list(session._question_timers.values()):
            if not task.done():
                task.cancel()
        session._question_timers.clear()

    async def _auto_answer_after(
        self, session: Session, question_id: str, timeout: float
    ) -> None:
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return  # the user (or a cleanup path) cancelled us first
        # The user might have answered during the sleep — re-check.
        if question_id not in session._pending_questions:
            return
        # Pop our own timer entry so _deliver_question_answer doesn't
        # try to cancel a task that's currently running (self).
        session._question_timers.pop(question_id, None)
        try:
            await self._deliver_question_answer(
                session, question_id, self.AUTO_ANSWER_TEXT, auto=True
            )
        except Exception:
            logger.exception(
                "Auto-answer for session %s question %s failed",
                session.id,
                question_id,
            )

    @staticmethod
    def _format_answers(
        questions: list[dict[str, Any]], answers: list[dict[str, Any]]
    ) -> str:
        """Render the user's answers as a string Claude can read.

        `answers` is a list aligned with `questions`; each entry has
        either {"selected": [labels]} or {"text": "free-form"}.
        """
        lines: list[str] = []
        for i, q in enumerate(questions):
            question_text = q.get("question", "")
            ans = answers[i] if i < len(answers) else {}
            if ans.get("text"):
                lines.append(f"Q: {question_text}\nA: {ans['text']}")
            else:
                selected = ans.get("selected") or []
                if isinstance(selected, str):
                    selected = [selected]
                lines.append(
                    f"Q: {question_text}\nA: {', '.join(selected) if selected else '(no answer)'}"
                )
        return "\n\n".join(lines)

    async def approve_tool(self, session_id: str, tool_use_id: str) -> bool:
        """Legacy SDK-era hook. The CLI-direct backend handles tool
        permissions internally, so this is effectively a no-op."""
        session = self.sessions.get(session_id)
        if not session:
            return False
        pending = session._pending_approvals.get(tool_use_id)
        if not pending or pending.future.done():
            return False
        pending.future.set_result(True)
        return True

    async def deny_tool(
        self, session_id: str, tool_use_id: str, reason: str = ""
    ) -> bool:
        """Legacy SDK-era hook. The CLI-direct backend handles tool
        permissions internally, so this is effectively a no-op."""
        session = self.sessions.get(session_id)
        if not session:
            return False
        pending = session._pending_approvals.get(tool_use_id)
        if not pending or pending.future.done():
            return False
        pending.future.set_result(False)
        return True
