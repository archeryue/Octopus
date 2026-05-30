"""Agent-to-agent delegation manager (agent-collaboration.md).

The shape mirrors `server/bg_tasks.py`: an asynchronous, fire-and-forget
operation on the parent agent's behalf, whose terminal result is
delivered back to the parent session as a *new* injected user message
(routed through `SessionManager.start_message`, the same path bg-task
results use). That parent-injection is what gives the parent agent a
fresh turn to react to the child's reply.

For agent collaboration, the operation is "spawn a child Session under
another agent and run one turn there". A delegation is **not** a new
persistence concept — it is a normal `Session` row with
``origin='delegation'`` and ``parent_session_id`` set. The delegation id
*is* the child session id; we don't invent a parallel id space.

What this module owns:

  - The in-memory registry of live (and recently-finished) delegations,
    keyed by child-session id.
  - The broadcast subscriber that watches the child's event stream and
    captures the events that matter for delivery (the same filter
    bridges use for quiet mode: assistant_text + result + error).
  - The cycle and depth guards that walk the parent chain.
  - The agent-name lookup (case-insensitive, ambiguity-rejecting).
  - The injection formatter: ``[agent-reply|agent-error:<name>
    delegation=<id>]`` plus the body, fed through
    ``SessionManager.start_message(parent_session_id, …)``.

What this module does NOT own:

  - The ``ask_agent`` MCP server (Phase 2). The server is a thin
    stdio shim that POSTs to the FastAPI routes which call into here.
  - The caller-aware ``ask`` server (Phase 3) — that's a one-line
    change in the existing question handler.
  - Frontend rendering (Phase 4).

Phase 1 deliberately limits itself to: a real child Session is created
under the target agent, the child runs its first turn, and the reply is
injected back into the parent. The parent agent has no tool with which
to invoke this yet — only the REST API does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .database import Database
    from .session_manager import SessionManager

logger = logging.getLogger(__name__)


# Maximum agent-delegation hops allowed in a single caller chain
# (agent-collaboration.md §5.9). The user is hop 0; each delegation
# (origin='delegation' session) is one hop. Allows root → A → B → C
# (three delegated agents under the human) but rejects deeper. A small
# constant on purpose — agent fan-out is meant to be shallow.
DEPTH_CAP = 3


class DelegationError(Exception):
    """Surface-level error for the REST layer. Carries an HTTP status so
    the routes can translate uniformly (404 for resolution failures,
    409 for guard violations)."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class DelegationRunState:
    """Live record of one delegation. Lifetime: from
    ``start_delegation`` to ``state != "running"`` plus a retention
    window so ``list_agent_tasks`` can show recently-finished items.
    """

    # The id is the child session id — see module docstring.
    delegation_id: str
    parent_session_id: str
    target_agent_id: str
    target_agent_name: str
    request: str
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    state: str = "running"  # "running" | "completed" | "failed" | "cancelled"
    captured_text: list[str] = field(default_factory=list)
    finished_at: str | None = None
    error: str | None = None
    # Defence-in-depth: terminal injection must fire at most once per
    # record. The state-flip-before-interrupt dance in
    # `cancel_delegation` already prevents the obvious race, but a
    # `result` and an `error` event can arrive close together from a
    # crashing child; this flag forces a single emission no matter
    # how the producers interleave.
    _terminal_injected: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        """API-shape for ``GET /sessions/{sid}/delegations`` and the
        ``ask_agent`` tool's return value. Hides ``captured_text``
        (that goes into the parent's transcript, not the JSON API)."""
        return {
            "delegation_id": self.delegation_id,
            "sub_session_id": self.delegation_id,
            "parent_session_id": self.parent_session_id,
            "target_agent_id": self.target_agent_id,
            "target_agent_name": self.target_agent_name,
            "request": self.request,
            "state": self.state,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


class DelegationManager:
    """In-process owner of delegation state.

    Wired in by ``main.py`` lifespan after ``SessionManager.initialize``:
    we register a broadcast listener so the per-session WS events the
    SessionManager already produces become our event source. No extra
    subprocess, no extra polling.
    """

    BROADCAST_KEY = "delegation_manager"

    def __init__(self) -> None:
        # Keyed by delegation_id (== child session id). Records stay
        # around after their terminal state so list_agent_tasks can
        # render recently-finished entries; a future LRU/retention
        # policy can trim this, but at single-user scale a flat dict
        # is fine.
        self._records: dict[str, DelegationRunState] = {}
        self.session_mgr: "SessionManager | None" = None
        self.db: "Database | None" = None

    # ------------------------------------------------------------ wiring

    def bind(self, session_mgr: "SessionManager", db: "Database") -> None:
        """Subscribe to the session manager's broadcast bus. Idempotent
        on repeat calls (last writer wins on the key)."""
        self.session_mgr = session_mgr
        self.db = db
        session_mgr.on_broadcast(self.BROADCAST_KEY, self._on_broadcast)

    def shutdown(self) -> None:
        if self.session_mgr is not None:
            self.session_mgr.remove_broadcast(self.BROADCAST_KEY)

    # --------------------------------------------------------- public API

    async def start_delegation(
        self,
        *,
        parent_session_id: str,
        agent_name: str,
        request: str,
        files: list[str] | None = None,
    ) -> DelegationRunState:
        """Create a child session under the named target agent and kick
        off its first turn. Returns immediately with the record — the
        reply arrives later via injection into the parent session
        (agent-collaboration.md §5.1)."""
        if self.session_mgr is None or self.db is None:
            raise DelegationError(
                "DelegationManager not bound", status_code=500
            )
        if not request or not request.strip():
            raise DelegationError(
                "request must be a non-empty string", status_code=400
            )

        parent = self.session_mgr.get_session(parent_session_id)
        if parent is None:
            raise DelegationError(
                f"Parent session {parent_session_id!r} not found",
                status_code=404,
            )

        target = await self._resolve_target_agent(agent_name)
        if target is None:
            raise DelegationError(
                f"No agent named {agent_name!r}", status_code=404
            )
        if parent.agent_id and target["id"] == parent.agent_id:
            raise DelegationError(
                "Cannot delegate to yourself — pick a different agent",
                status_code=409,
            )

        self._check_chain(parent, target_agent_id=target["id"])

        # Parent name is informational only (used in the child's first
        # message and in the injection prefix). A missing parent agent
        # falls back to a generic placeholder rather than 500ing.
        parent_agent = (
            await self.db.get_agent(parent.agent_id) if parent.agent_id else None
        )
        parent_name = (parent_agent or {}).get("name") or "another agent"

        child_backend = (target.get("backend") or "claude-code")
        child_name = f"{target['name']} ← {parent_name}"
        child = await self.session_mgr.create_session(
            agent_id=target["id"],
            name=child_name,
            working_dir=parent.working_dir,
            origin="delegation",
            backend=child_backend,
            parent_session_id=parent.id,
            delegation_request=request,
        )

        rec = DelegationRunState(
            delegation_id=child.id,
            parent_session_id=parent.id,
            target_agent_id=target["id"],
            target_agent_name=target["name"],
            request=request,
        )
        # Register BEFORE start_message: the broadcast may fire
        # synchronously from inside start_message on a fast harness
        # (test fakes, especially), and the listener needs to find
        # us in _records.
        self._records[child.id] = rec

        composed = self._compose_initial_prompt(
            parent_name=parent_name,
            parent_session_id=parent.id,
            request=request,
            files=files or [],
            working_dir=parent.working_dir,
        )
        try:
            await self.session_mgr.start_message(child.id, composed)
        except Exception as exc:
            # Surface as failure on the record so the parent still
            # gets a terminal injection rather than a phantom run.
            logger.exception(
                "Failed to start child session %s for delegation", child.id
            )
            rec.state = "failed"
            rec.error = f"failed to start child session: {exc}"
            rec.finished_at = datetime.now(timezone.utc).isoformat()
            await self._inject_terminal(rec)
            raise DelegationError(
                f"failed to start delegation: {exc}", status_code=500
            ) from exc

        return rec

    async def cancel_delegation(
        self, delegation_id: str, *, reason: str | None = None
    ) -> DelegationRunState:
        """Stop a running delegation. Idempotent — cancelling a finished
        delegation is a no-op that returns the existing record."""
        if self.session_mgr is None:
            raise DelegationError(
                "DelegationManager not bound", status_code=500
            )
        rec = self._records.get(delegation_id)
        if rec is None:
            raise DelegationError(
                f"No delegation {delegation_id!r}", status_code=404
            )
        if rec.state != "running":
            return rec

        # CRITICAL: transition the record state BEFORE calling
        # `interrupt()`. The interrupt broadcasts an `error` event
        # that our own `_on_broadcast` would otherwise turn into a
        # spurious `[agent-error reason=child error]` injection,
        # which would race with the `[agent-error reason=cancelled]`
        # we inject below — the parent would see two terminal turns
        # for one cancellation. The `_on_broadcast` running-check
        # guards against that as long as we flip state first.
        rec.state = "cancelled"
        rec.error = reason or "cancelled by caller"
        rec.finished_at = datetime.now(timezone.utc).isoformat()
        try:
            await self.session_mgr.interrupt(delegation_id)
        except Exception:
            logger.exception(
                "interrupt(%s) raised during cancellation", delegation_id
            )
        await self._inject_terminal(rec)
        return rec

    def list_delegations(
        self, parent_session_id: str, *, limit: int = 25
    ) -> list[DelegationRunState]:
        """Recent delegations spawned by a parent session, newest first."""
        rows = [
            r
            for r in self._records.values()
            if r.parent_session_id == parent_session_id
        ]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[:limit]

    def get_delegation(self, delegation_id: str) -> DelegationRunState | None:
        return self._records.get(delegation_id)

    # ----------------------------------------------------------- internals

    async def _resolve_target_agent(
        self, name: str
    ) -> dict[str, Any] | None:
        """Case-insensitive name lookup over non-archived agents.

        Multiple matches → DelegationError(409). One match → the row.
        Zero matches → None (caller turns this into a 404)."""
        assert self.db is not None  # bound at lifespan
        wanted = (name or "").strip().lower()
        if not wanted:
            return None
        agents = await self.db.load_agents()
        matches = [a for a in agents if (a.get("name") or "").lower() == wanted]
        if not matches:
            return None
        if len(matches) > 1:
            names = ", ".join(repr(a.get("name")) for a in matches)
            raise DelegationError(
                f"Ambiguous agent name {name!r} (matches: {names})",
                status_code=409,
            )
        return matches[0]

    def _check_chain(self, parent, *, target_agent_id: str) -> None:
        """Walk the parent chain upward from the given parent session.

        Enforces two rules (agent-collaboration.md §5.9):
        - Cycle: the target agent must not already appear in the chain.
        - Depth: the new delegation (counted as +1) plus all existing
          ``origin='delegation'`` hops in the chain must not exceed
          ``DEPTH_CAP``.

        Self-delegation (target == parent.agent_id) is rejected by the
        caller; this method still catches it transitively via the cycle
        check, but the dedicated error message at the call site is
        clearer for the user.

        Fail-closed semantics: a corrupted parent_session_id chain
        (loop in the session-id pointers, or a chain longer than the
        safety cap, or a non-null ``parent_session_id`` whose target
        isn't live) is rejected as a 409 rather than silently treated
        as a valid (short) chain. The cycle guard is what stands
        between "Vera asks Octo" and an infinite delegation tower —
        it must never be skipped.
        """
        assert self.session_mgr is not None
        chain_agent_ids: set[str] = set()
        visited_session_ids: set[str] = set()
        existing_hops = 0
        # Generous walk cap — much larger than DEPTH_CAP — chosen to
        # cover the future where we relax depth without re-tuning this
        # constant. Cap exhaustion is treated as corruption (see below)
        # so a runaway loop can't camp the route forever.
        _SAFETY_CAP = 64
        sid: str | None = parent.id
        for _ in range(_SAFETY_CAP):
            if sid is None:
                break
            if sid in visited_session_ids:
                # Real session-id cycle (e.g. A.parent=B, B.parent=A)
                # — not a transitive agent cycle, but a corrupted
                # pointer chain. Either way this is unsafe; reject.
                raise DelegationError(
                    "Caller chain has a session-id cycle "
                    "(corrupted parent_session_id pointers)",
                    status_code=409,
                )
            visited_session_ids.add(sid)
            session = self.session_mgr.get_session(sid)
            if session is None:
                # The chain references a session we can't see in
                # memory. For the root walk-back this is normal (sid
                # was None already, handled above) — but if sid was
                # non-null and lookup failed, the chain is incomplete
                # and we can't make a sound depth/cycle decision.
                raise DelegationError(
                    "Caller chain references a session that no "
                    "longer exists; refuse rather than guess",
                    status_code=409,
                )
            if session.agent_id:
                chain_agent_ids.add(session.agent_id)
            if session.origin == "delegation":
                existing_hops += 1
            sid = session.parent_session_id
        else:
            # for/else: loop exhausted the safety cap without
            # encountering a None terminator. That's a chain longer
            # than _SAFETY_CAP, which is unreasonable in practice —
            # treat as corruption and fail closed.
            raise DelegationError(
                f"Caller chain exceeds {_SAFETY_CAP} hops; refuse "
                f"as a fail-closed guard against pointer corruption",
                status_code=409,
            )

        if target_agent_id in chain_agent_ids:
            raise DelegationError(
                "Cycle rejected: target agent already in the caller chain",
                status_code=409,
            )
        # +1 because the new delegation we're about to create is the
        # next hop. The cap is "no more than N delegation hops in any
        # one caller chain" (root user is not counted; only delegations).
        if existing_hops + 1 > DEPTH_CAP:
            raise DelegationError(
                f"Delegation depth would exceed {DEPTH_CAP} hops",
                status_code=409,
            )

    def _compose_initial_prompt(
        self,
        *,
        parent_name: str,
        parent_session_id: str,
        request: str,
        files: list[str],
        working_dir: str,
    ) -> str:
        """The child's first user message. Names the caller, ships the
        request verbatim, and optionally lists files the parent flagged
        as relevant.

        File paths are resolved against ``working_dir`` (plan §5.7):
        absolute paths pass through unchanged; relative ones get joined
        to the working dir. We also check existence and clearly flag
        missing entries — better the child sees ``(not found)`` than a
        misleading absolute path that doesn't exist on disk.

        We do NOT include any of the parent's transcript — that's a
        deliberate scope/privacy boundary (plan §2)."""
        from pathlib import Path

        lines = [
            f"You were asked by agent **{parent_name}** "
            f"(session `{parent_session_id}`).",
            "Their request follows.",
            "",
            "---",
            request.strip(),
        ]
        if files:
            lines.append("")
            lines.append(
                "The caller flagged these files as relevant "
                f"(paths resolved against `{working_dir}`):"
            )
            base = Path(working_dir)
            for raw in files:
                resolved = (
                    Path(raw) if Path(raw).is_absolute() else base / raw
                )
                try:
                    resolved = resolved.resolve()
                except OSError:
                    # Path resolution failed (e.g. permission error
                    # walking a symlink); surface the raw form so the
                    # child still sees what the parent meant.
                    lines.append(f"- {raw}  (could not resolve)")
                    continue
                if resolved.exists():
                    lines.append(f"- {resolved}")
                else:
                    lines.append(f"- {resolved}  (not found)")
        return "\n".join(lines)

    # ----------------------------------------------- broadcast → injection

    async def _on_broadcast(self, msg: dict[str, Any]) -> None:
        """Filter the session-manager broadcast bus to delegation
        children we're tracking. Mirrors the bridge quiet-mode filter:
        capture assistant_text, finalise on result, route error through
        the failure injection path, and route `question_request` up to
        the parent so the parent's model gets a turn to answer it
        (agent-collaboration.md §5.4 — the caller chain rule)."""
        sid = msg.get("session_id")
        if not sid:
            return
        rec = self._records.get(sid)
        if rec is None or rec.state != "running":
            return

        kind = msg.get("type")
        if kind == "assistant_text":
            text = msg.get("content")
            if isinstance(text, str) and text:
                rec.captured_text.append(text)
            return
        if kind == "question_request":
            await self._inject_question(rec, msg)
            return
        if kind == "result":
            if msg.get("is_error"):
                rec.state = "failed"
                rec.error = "child session reported an error result"
            else:
                rec.state = "completed"
            rec.finished_at = datetime.now(timezone.utc).isoformat()
            await self._inject_terminal(rec)
            return
        if kind == "error":
            rec.state = "failed"
            rec.error = str(msg.get("message") or "child session error")
            rec.finished_at = datetime.now(timezone.utc).isoformat()
            await self._inject_terminal(rec)
            return

    async def _inject_question(
        self, rec: DelegationRunState, msg: dict[str, Any]
    ) -> None:
        """Bubble a child's `question_request` up to the parent session
        as an injected `[agent-question:…]` turn. The pending question
        itself stays on the child's session (the existing UI path can
        still answer it manually); the parent's
        `answer_agent_question(delegation_id, choice)` tool drains that
        same queue on success — first to drain wins.
        """
        assert self.session_mgr is not None
        question_id = msg.get("question_id") or ""
        questions = msg.get("questions") or []
        body = self._render_question_body(questions)
        prompt = (
            f"[agent-question:{rec.target_agent_name} "
            f"delegation={rec.delegation_id} "
            f"question_id={question_id}]\n{body}\n\n"
            f"Decide: answer them by calling "
            f"`mcp__ask_agent__answer(delegation_id="
            f"\"{rec.delegation_id}\", choice=…)`; or, if you don't "
            f"know, use your own `mcp__ask__user` to ask the user "
            f"and forward their answer; or cancel via "
            f"`mcp__ask_agent__cancel`."
        )
        try:
            await self.session_mgr.start_message(
                rec.parent_session_id, prompt
            )
        except Exception:
            logger.exception(
                "Failed to inject delegation %s question into parent %s",
                rec.delegation_id,
                rec.parent_session_id,
            )

    @staticmethod
    def _render_question_body(questions: list[dict[str, Any]]) -> str:
        """Render the AskUserQuestion payload into a human-readable
        block the parent's model can reason over. We render every
        question in the batch, but answering currently applies the
        parent's `choice` to the FIRST question only — see
        `answer_pending_question`."""
        if not questions:
            return "(no question text — child sent an empty payload)"
        lines: list[str] = []
        for i, q in enumerate(questions, start=1):
            qtext = q.get("question") or "(no question text)"
            header = q.get("header")
            multi = bool(q.get("multiSelect"))
            options = q.get("options") or []
            lines.append(f"Question {i}: {qtext}")
            if header:
                lines.append(f"  (header: {header})")
            for opt in options:
                label = (opt or {}).get("label") or "?"
                desc = (opt or {}).get("description")
                if desc:
                    lines.append(f"  - {label}: {desc}")
                else:
                    lines.append(f"  - {label}")
            mode = "multi-select" if multi else "single-choice"
            lines.append(f"  ({mode}; pass the chosen label as `choice`.)")
        return "\n".join(lines)

    async def answer_pending_question(
        self, delegation_id: str, choice: str
    ) -> dict[str, Any]:
        """Drain the oldest pending question on the delegation's child
        session by feeding it the parent's chosen label.

        Multiple-question batches are rare (the ask MCP tool accepts
        1-4 questions per call; the common shape is 1). When there
        are >1 we apply `choice` to the first question and leave the
        rest with empty `selected` — same defaulting the human UI
        does when the user skips an option."""
        if self.session_mgr is None:
            raise DelegationError(
                "DelegationManager not bound", status_code=500
            )
        rec = self._records.get(delegation_id)
        if rec is None:
            raise DelegationError(
                f"No delegation {delegation_id!r}", status_code=404
            )
        if rec.state != "running":
            raise DelegationError(
                f"Delegation {delegation_id!r} is {rec.state} — no "
                f"pending question to answer",
                status_code=409,
            )
        child = self.session_mgr.get_session(rec.delegation_id)
        if child is None:
            raise DelegationError(
                "Child session no longer alive", status_code=404
            )
        if not child._pending_questions:
            raise DelegationError(
                "No pending question on the child session",
                status_code=409,
            )
        question_id, pending = next(iter(child._pending_questions.items()))
        choice = (choice or "").strip()
        if not choice:
            raise DelegationError(
                "`choice` must be a non-empty string", status_code=400
            )
        answers = [{"selected": [choice], "text": None}]
        # Pad multi-question batches with empty selections; same shape
        # the frontend submits when a user clicks-through without
        # answering some entries.
        for _ in pending.questions[1:]:
            answers.append({"selected": [], "text": None})
        ok = await self.session_mgr.answer_question(
            rec.delegation_id, question_id, answers
        )
        if not ok:
            # Race: the human UI answered between our get and our set.
            raise DelegationError(
                "Question already answered by another path",
                status_code=409,
            )
        return {
            "delegation_id": delegation_id,
            "question_id": question_id,
            "choice": choice,
            "ok": True,
        }

    async def _inject_terminal(self, rec: DelegationRunState) -> None:
        """Push the terminal turn into the parent session.

        Routed through ``start_message`` so it queues behind any
        in-flight parent turn (the same property bg-task delivery
        relies on). The marker prefix is structured text so the
        parent's model can disambiguate when multiple delegations are
        live concurrently, and so the frontend can detect and render
        it as a special card once Phase 4 lands.

        Idempotent. The ``_terminal_injected`` flag ensures that
        even if two terminal-producing events race (a `result` from
        the child + a `cancel_delegation` from the parent, or a
        `result` and an `error` from a crashing child), exactly one
        ``[agent-…]`` turn lands in the parent session.
        """
        assert self.session_mgr is not None
        if rec._terminal_injected:
            return
        rec._terminal_injected = True
        if rec.state == "completed":
            body = ("".join(rec.captured_text)).strip()
            if not body:
                body = "(child session ended without producing any text)"
            prompt = (
                f"[agent-reply:{rec.target_agent_name} "
                f"delegation={rec.delegation_id}]\n{body}"
            )
        else:
            reason = rec.error or rec.state
            prompt = (
                f"[agent-error:{rec.target_agent_name} "
                f"delegation={rec.delegation_id} reason={reason}]\n"
                f"(child session ended in state {rec.state!r})"
            )
        try:
            await self.session_mgr.start_message(
                rec.parent_session_id, prompt
            )
        except Exception:
            # The parent may have been deleted while the child was
            # running; that's a tolerable race — we just log it. The
            # record stays in our registry so the API can still report
            # the terminal state.
            logger.exception(
                "Failed to inject delegation %s terminal into parent %s",
                rec.delegation_id,
                rec.parent_session_id,
            )


# Module-level singleton (mirrors the session_manager / bg_tasks
# pattern). Wired in main.py's lifespan with .bind(...).
delegation_manager = DelegationManager()
