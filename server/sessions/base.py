from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import settings
from ..database import Database
from ..harness import (
    HarnessCredential,
    HarnessEvent,
    HarnessRun,
    get_harness,
)
from ..models import (
    MessageContent,
    SessionStatus,
)
from ..oauth_errors import RefreshErrorCode

logger = logging.getLogger(__name__)

# How often streamed token deltas are flushed to the client as one WS frame
# (inline-steering.md §4 S1). 50ms is ~20 updates/second: below the rate at
# which a reader perceives stepping, and ~10x fewer frames and renders than
# forwarding every token individually.
_DELTA_FLUSH_SECONDS = 0.05

# How many sub-agent cards a session keeps live state for. They're UI state
# (the transcript holds the durable record), so this only stops a session that
# fans out all day from growing a map forever.
_MAX_SUBAGENTS_PER_SESSION = 64

# Holding a finished CLI process makes the next turn ~1.5s faster and keeps its
# prompt cache warm, but costs ~255MB of RSS for as long as it's held
# (inline-steering.md §7). Two bounds keep that honest: a process is dropped
# after this long without a turn, and only this many are held at once (the
# least-recently-used goes first). A dropped process is never a broken
# session — the next turn spawns and `--resume`s, which is what every turn did
# before reuse existed.
_HELD_PROCESS_IDLE_SECONDS = 600.0
_MAX_HELD_PROCESSES = 4
_REAPER_INTERVAL_SECONDS = 30.0
# How long to wait for one held process to die before giving up on it.
_HELD_STOP_TIMEOUT = 2.0
# How many un-written steers a single turn will hold. It's a person typing;
# beyond this the message queues for the next turn instead of being refused.
_MAX_PENDING_STEERS = 8


class ForkError(Exception):
    """A fork request was rejected for a reason the route maps to a status
    code (session-rewind.md §5.1). `reason` is a stable machine token;
    `status_code` is the HTTP status the route should return."""

    def __init__(self, message: str, *, reason: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code


def resolve_working_dir(working_dir: str | None) -> str:
    """Freeze a session's working directory to an ABSOLUTE path at creation.

    A session's conversation + memory live under a path Claude derives from
    its working directory (``projects/<cwd-slug>/``). If we stored the dir
    relative (``.``, ``Octopus``), the slug would be re-resolved against the
    *server process's* cwd on every turn — so the storage location would
    depend on where/how the server happens to be launched. That ambient
    coupling silently relocates (and orphans) a session's history whenever
    the server's cwd differs: a manual launch from another dir, an edited
    systemd unit, or a cloud deployment with a different pwd.

    Resolving to absolute once, here, removes ``os.getcwd()`` from the
    equation forever after: the slug becomes a pure function of session-owned
    data. Relative input is interpreted against the server cwd this one time
    (the natural meaning of a path the caller typed), then frozen.
    """
    raw = working_dir or settings.default_working_dir
    return str(Path(raw).expanduser().resolve())


def _session_fork_kwargs(row: dict[str, Any]) -> dict[str, Any]:
    """Extract the six persisted fork columns from a `load_sessions` row dict
    into Session(**…) kwargs. Centralised so every Session-from-row site stays
    in lock-step (session-rewind.md §4)."""
    return {
        "forked_from_session_id": row.get("forked_from_session_id"),
        "fork_after_seq": row.get("fork_after_seq"),
        "fork_needs_replay": bool(row.get("fork_needs_replay")),
        "fork_metadata": row.get("fork_metadata"),
        "fork_revert_record": row.get("fork_revert_record"),
        "fork_status": row.get("fork_status"),
    }


def fork_info_fields(
    *,
    backend: str,
    forked_from_session_id: str | None,
    fork_after_seq: int | None,
    fork_metadata: str | None,
    fork_revert_record: str | None,
) -> dict[str, Any]:
    """The fork-related fields exposed on `SessionInfo`
    (session-rewind.md §4). `can_fork` comes from the harness profile;
    `fork_prefilled_prompt` is read out of the ephemeral `fork_metadata` blob
    (while non-null); `fork_revert_record` is the durable revert outcome.
    fork_status / fork_needs_replay / the raw blob stay server-internal."""
    try:
        can_fork = get_harness(backend).profile.can_fork
    except Exception:
        can_fork = False
    prefilled: str | None = None
    full_copy = False
    if fork_metadata:
        try:
            meta = json.loads(fork_metadata)
            prefilled = meta.get("prefilled_prompt")
            full_copy = bool(meta.get("full_copy"))
        except (json.JSONDecodeError, AttributeError):
            prefilled = None
    revert: dict[str, Any] | None = None
    if fork_revert_record:
        try:
            revert = json.loads(fork_revert_record)
        except json.JSONDecodeError:
            revert = None
    return {
        "can_fork": can_fork,
        "forked_from_session_id": forked_from_session_id,
        "fork_after_seq": fork_after_seq,
        "fork_prefilled_prompt": prefilled,
        "fork_revert_record": revert,
        # A /fork copy-dir duplicate vs a /rewind branch — the UI renders the
        # fork banner / sidebar badge differently (session-fork.md).
        "fork_is_full_copy": full_copy,
    }


@dataclass
class QueuedPrompt:
    """A user turn waiting to run.

    Carries both the raw prompt text and any attachments the user
    uploaded with it — we resolve attachments → absolute paths only at
    spawn time (not at enqueue time) so the agent sees the same prompt
    shape regardless of whether the turn ran immediately or after a
    queue drain.
    """

    prompt: str
    attachment_ids: list[str]


@dataclass
class PendingApproval:
    """Held for legacy WS approve_tool/deny_tool messages.

    The CLI-direct backend handles tool permissions itself via the control
    protocol, so we don't populate this from the new code path — it's
    retained only so existing WS clients don't get errors on the old
    message types.
    """

    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str
    future: asyncio.Future


@dataclass
class PendingQuestion:
    """Mirror of an AskUserQuestion the backend is currently asking us.

    The backend owns the actual control-protocol future; this is just the
    info we surface to the UI so reload-on-reconnect can re-render the form.
    """

    question_id: str
    questions: list[dict[str, Any]]


@dataclass
class Session:
    id: str
    name: str
    working_dir: str
    status: SessionStatus = SessionStatus.idle
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    claude_session_id: str | None = None
    credential_id: str | None = None
    # Owning agent (agent-refactor.md). agent_id is required for any session
    # created post-refactor; left optional on the dataclass so legacy
    # in-memory construction paths don't break mid-migration.
    agent_id: str | None = None
    # Who created this session: 'user' | 'schedule' | 'delegation' | 'fork' |
    # 'application' | 'app'. Scheduler fires auto-archive on idle (§5.6); user
    # sessions
    # persist. 'delegation' sessions auto-archive on idle too — they're a
    # transient child spawned by an agent-to-agent ask_agent call
    # (agent-collaboration.md §5.2).
    origin: str = "user"
    # Which AI backend drives this session ('claude-code' | 'codex').
    backend: str = "claude-code"
    # Agent-to-agent: parent session that spawned this delegation, or None
    # for every non-delegation session. Used by the delegation listener to
    # route replies/questions/errors back to the parent and by guards to
    # walk the caller chain (cycle + depth). (agent-collaboration.md §4.1)
    parent_session_id: str | None = None
    # The original delegation prompt, kept verbatim for UI display on
    # delegation sessions. NULL elsewhere.
    delegation_request: str | None = None
    # The application that owns this session — its build session, or a
    # conversation the running app is holding with an agent (origin='app').
    # NULL on every ordinary session. (app-agent-access.md §3)
    app_id: str | None = None
    # Live sub-agent runs, keyed by the tool call that spawned them
    # (native-subagents.md §4). Broadcast-only state: the durable record is
    # the Task / collab tool call already in the transcript, so this exists
    # to keep a card alive across a browser reload, not across a restart.
    _subagents: dict[str, Any] = field(default_factory=dict, repr=False)
    # Session tree-rewind / fork (session-rewind.md §4). All NULL/False
    # on non-fork sessions. fork_metadata / fork_revert_record hold raw JSON
    # strings (parsed lazily); fork_status drives crash recovery.
    forked_from_session_id: str | None = None
    fork_after_seq: int | None = None
    fork_needs_replay: bool = False
    fork_metadata: str | None = None
    fork_revert_record: str | None = None
    fork_status: str | None = None
    _message_count: int = field(default=0, repr=False)
    # Set True for the lifetime of a fork-create saga against this session as
    # the PARENT (session-rewind.md §5.4). start_message() refuses while
    # set; cleared in fork_session's finally. A real mutex even though
    # start_message sets _active_task without holding _lock.
    _forking: bool = field(default=False, repr=False)
    _active_task: asyncio.Task | None = field(default=None, repr=False)
    # Per-prompt task that interrupt() targets; the outer _active_task is
    # the orchestrator loop and survives interrupts so it can drain the queue.
    _inner_task: asyncio.Task | None = field(default=None, repr=False)
    _backend: HarnessRun | None = field(default=None, repr=False)
    # When the held CLI process last finished a turn — the reaper's clock
    # (inline-steering.md §7). None whenever no process is being held.
    _held_run_at: float | None = field(default=None, repr=False)
    _pending_approvals: dict[str, PendingApproval] = field(default_factory=dict, repr=False)
    _pending_questions: dict[str, PendingQuestion] = field(default_factory=dict, repr=False)
    # question_id -> background timer that auto-answers if the user
    # never replies (see SessionManager._schedule_question_timeout).
    _question_timers: dict[str, asyncio.Task] = field(default_factory=dict, repr=False)
    # AUQ delivery coordination for the new MCP-based flow. The
    # `mcp__ask__user` tool (server/mcp_servers/ask.py) creates a
    # pending question via REST, then HTTP-long-polls the answer
    # endpoint, which awaits the Event below. The user's UI submit
    # sets `_pending_question_answers[q_id]` and signals the Event;
    # the long-poll unblocks and returns the answer to the MCP tool,
    # which returns it as the tool result so the model can continue.
    # Replaces the old --permission-prompt-tool=stdio deny-channel
    # hack that exposed us to the CLI's premature-exit bug.
    _pending_question_events: dict[str, asyncio.Event] = field(default_factory=dict, repr=False)
    _pending_question_answers: dict[str, str] = field(default_factory=dict, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _pending_queue: list[QueuedPrompt] = field(default_factory=list, repr=False)
    # Steers: messages typed while THIS turn is running, to be handed to the
    # CLI mid-turn rather than queued behind the turn (inline-steering.md §8).
    # The turn's writer task drains this; `_steer_ready` is how it learns
    # there's something to write without waiting for the next event — during a
    # long tool call no events arrive at all, and a slow tool is exactly when
    # someone reaches for the keyboard.
    _steer_queue: list[QueuedPrompt] = field(default_factory=list, repr=False)
    _steer_ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    # Set while a turn can still accept a steer: from the moment the stream
    # starts until `result` is seen. Checked under `_steer_lock` so accepting a
    # steer and closing the window can't interleave.
    _steer_open: bool = field(default=False, repr=False)
    _steer_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class SessionManagerBase:
    """Shared state and the broadcast bus every other slice stands on.

    Holds the session map, the subscriber list, the injected collaborators and
    the tuning constants. A mixin inherits this rather than declaring the
    attributes it borrows, so the type checker knows what it may touch — the
    lesson from the database split, where a method-only partition silently
    dropped the class constants and `Database` ended up with no `initialize`.
    """

    # ---------------------------------------------------------------- contracts
    #
    # What one slice calls on another. Declared here rather than left implicit
    # because the split makes the dependency real: `turns` in particular reaches
    # into credentials, processes, events, outcomes and questions, which is the
    # honest shape of driving a turn and worth being able to see in one place.
    #
    # Every one is overridden by the mixin that owns it — the mixins precede
    # this class in the MRO — so these bodies never run. They exist so the type
    # checker can verify the calls, which is the check that caught the database
    # split dropping its class constants.

    async def _blit_attachments_to_descendant_forks(self, session_id: str) -> None:
        raise NotImplementedError  # provided by a mixin

    def _cancel_all_question_timers(self, session: Session) -> None:
        raise NotImplementedError  # provided by a mixin

    async def _clear_fork_first_turn_state(self, session: Session) -> None:
        raise NotImplementedError  # provided by a mixin

    async def _enforce_held_cap(self, *, keep_session_id: str | None = None) -> int:
        raise NotImplementedError  # provided by a mixin

    @staticmethod
    def _event_to_message_content(event: HarnessEvent) -> MessageContent | None:
        raise NotImplementedError  # provided by a mixin

    @staticmethod
    def _event_to_ws_message(session_id: str, event: HarnessEvent) -> dict[str, Any] | None:
        raise NotImplementedError  # provided by a mixin

    def _flush_text_deltas(
        self, session_id: str, buf: list[str]
    ) -> dict[str, Any] | None:
        raise NotImplementedError  # provided by a mixin

    def _forget_backend(self, session: Session) -> None:
        raise NotImplementedError  # provided by a mixin

    async def _load_agent(self, session: Session) -> dict[str, Any] | None:
        raise NotImplementedError  # provided by a mixin

    def _make_run(
        self,
        session: Session,
        agent: dict[str, Any] | None = None,
        connectors: list[tuple[Any, Any]] | None = None,
    ) -> HarnessRun:
        raise NotImplementedError  # provided by a mixin

    async def _mark_needs_reconnect(
        self, credential_id: str, code: RefreshErrorCode
    ) -> None:
        raise NotImplementedError  # provided by a mixin

    async def _persist_message(
        self,
        session: Session,
        msg: MessageContent,
        *,
        git_head: str | None = None,
        git_status_clean: bool | None = None,
    ) -> int | None:
        raise NotImplementedError  # provided by a mixin

    @staticmethod
    def _record_subagent(session: Session, update: Any) -> None:
        raise NotImplementedError  # provided by a mixin

    async def _recover_incomplete_forks(self) -> None:
        raise NotImplementedError  # provided by a mixin

    async def _resolve_credential(
        self, session: Session, agent: dict[str, Any] | None, harness
    ) -> HarnessCredential | None:
        raise NotImplementedError  # provided by a mixin

    def _reusable_run(
        self,
        session: Session,
        working_dir: str,
        credential: Any,
        agent: dict[str, Any] | None,
        connectors: list[tuple[Any, Any]] | None,
    ) -> HarnessRun | None:
        raise NotImplementedError  # provided by a mixin

    async def _safe_backend_interrupt(self, backend: HarnessRun) -> None:
        raise NotImplementedError  # provided by a mixin

    def _schedule_question_timeout(self, session: Session, question_id: str) -> None:
        raise NotImplementedError  # provided by a mixin

    def _start_turn_watchdog(
        self, backend: HarnessRun, state: dict[str, Any]
    ) -> asyncio.Task | None:
        raise NotImplementedError  # provided by a mixin

    async def _surface_auth_expiry(
        self, session: Session, *, cred_id: str | None, backend: str
    ) -> dict[str, Any]:
        raise NotImplementedError  # provided by a mixin

    async def _surface_stale_session(
        self, session: Session, *, backend: str, replayed: int = 0, omitted: int = 0
    ) -> dict[str, Any]:
        raise NotImplementedError  # provided by a mixin

    async def _surface_transient_exhausted(
        self, session: Session, *, backend: str, attempts: int
    ) -> dict[str, Any]:
        raise NotImplementedError  # provided by a mixin

    async def _surface_transient_retry(
        self, session: Session, *, attempt: int, max_attempts: int, delay: float
    ) -> dict[str, Any]:
        raise NotImplementedError  # provided by a mixin

    async def _surface_turn_timeout(
        self, session: Session, *, reason: str, limit: int, backend: str
    ) -> dict[str, Any]:
        raise NotImplementedError  # provided by a mixin

    async def auto_archive_scheduled_session(self, session_id: str) -> bool:
        raise NotImplementedError  # provided by a mixin

    async def resolve_credential_by_id(
        self,
        cred_id: str | None,
        *,
        style: str = "env_secret",
        context: str = "",
        require_auth: bool = True,
    ) -> HarnessCredential | None:
        raise NotImplementedError  # provided by a mixin

    _DURABLE_FORK_META_KEYS = ("full_copy", "duplicated_from")
    _AUTO_ARCHIVE_ORIGINS = ("schedule",)
    _AUTO_ARCHIVE_ELIGIBLE = ("schedule", "delegation")
    _MAX_RECOVERY_ATTEMPTS = 1
    _MAX_TRANSIENT_RETRIES = 2
    _TRANSIENT_RETRY_BASE_DELAY = 1.0  # seconds; doubles each attempt
    _OAUTH_REFRESH_LEEWAY_SEC = 300
    _BACKEND_DISPLAY = {"claude-code": "Claude Code", "codex": "Codex"}
    AUTO_ANSWER_TEXT = (
        "No human is available to answer this question right now. "
        "Proceed with the task autonomously and try hard to finish it without "
        "asking again. Make the most reasonable choice and continue.\n\n"
        "Only stop and leave a clear note describing what you would have done "
        "if the next action is genuinely risky or irreversible — for example: "
        "destroying data, force-pushing or rewriting shared git history, "
        "deploying to production, modifying billing/payments, sending "
        "messages or emails to external recipients, or running commands that "
        "affect shared infrastructure. For everything else (ambiguous design "
        "choices, formatting, library picks, small refactors), pick the most "
        "reasonable option and keep going."
    )

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self._broadcast_callbacks: dict[str, Callable] = {}
        self.db: Database | None = None
        # Background task that drops idle held CLI processes.
        self._reaper_task: asyncio.Task[None] | None = None
        # Wired in by main.py once the manager is constructed. Kept as
        # an opaque object — we only call `.fire(event)` on it — so the
        # session manager doesn't take a hard dependency on the
        # notifiers package's import surface.
        self._notifier_manager: Any = None
        # Likewise wired in by main.py — the ScheduleRunner. Used to
        # re-register jobs when archiving a session repoints the schedules
        # anchored to it. Opaque: we only call `.reschedule(row)`.
        self._schedule_runner: Any = None

    def set_notifier_manager(self, mgr: Any) -> None:
        self._notifier_manager = mgr

    def set_schedule_runner(self, runner: Any) -> None:
        self._schedule_runner = runner

    async def initialize(self, db: Database) -> None:
        self.db = db
        rows = await db.load_sessions()
        for row in rows:
            session = Session(
                id=row["id"],
                name=row["name"],
                working_dir=row["working_dir"],
                created_at=row["created_at"],
                claude_session_id=row["claude_session_id"],
                credential_id=row.get("credential_id"),
                agent_id=row.get("agent_id"),
                origin=row.get("origin") or "user",
                backend=row.get("backend") or "claude-code",
                parent_session_id=row.get("parent_session_id"),
                delegation_request=row.get("delegation_request"),
                app_id=row.get("app_id"),
                **_session_fork_kwargs(row),
            )
            session._message_count = await db.count_messages(session.id)
            self.sessions[session.id] = session
        logger.info("Loaded %d sessions from database", len(rows))
        # Sweep forks left mid-saga by a crash (session-rewind.md §5.6.7).
        await self._recover_incomplete_forks()
        # Sweep delegation children orphaned by a restart (agent-collaboration.md §5.2).
        await self._recover_orphaned_delegations()

    def on_broadcast(self, key: str, callback: Callable) -> None:
        self._broadcast_callbacks[key] = callback

    def remove_broadcast(self, key: str) -> None:
        self._broadcast_callbacks.pop(key, None)

    async def _broadcast(self, message: dict) -> None:
        for cb in list(self._broadcast_callbacks.values()):
            try:
                await cb(message)
            except Exception:
                logger.exception("Broadcast callback error")

    def list_sessions(self) -> list[Session]:
        return list(self.sessions.values())

    def get_session(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    async def _recover_orphaned_delegations(self) -> None:
        """Archive delegation children left live by a restart
        (agent-collaboration.md §5.2).

        Delegation run records live ONLY in `DelegationManager._records`
        (in-memory; never persisted — "the delegation id IS the child
        session id, no parallel id space"). A restart wipes that registry,
        and the child's subprocess is dead, so the chain can never finish:
        no `result`/`error` will ever arrive to drive `_inject_terminal`,
        and therefore nothing will ever auto-archive the child. Loaded back
        by `initialize` as a live `origin == "delegation"` session, it would
        otherwise sit forever in the sidebar's "+N delegations hidden"
        count with no path to cleanup.

        Any delegation-origin session that reaches this boot un-archived is
        by definition abandoned (a healthy one archives itself the moment
        its terminal turn is delivered, before the process ever exits). So
        sweep them all into the archive — they stay browsable via the
        account-menu manage page, exactly like a normal terminal delegation.
        Idempotent: a clean boot finds none. Pure session lifecycle — no
        backend specifics, so no harness involvement."""
        orphans = [
            sid for sid, s in self.sessions.items() if s.origin == "delegation"
        ]
        archived = 0
        for sid in orphans:
            try:
                if await self.auto_archive_scheduled_session(sid):
                    archived += 1
            except Exception:
                logger.exception(
                    "delegation recovery: failed to archive orphan %s", sid
                )
        if archived:
            logger.info(
                "delegation recovery: archived %d orphaned delegation session(s)",
                archived,
            )

    async def _fire_session_idle_notification(self, session: Session) -> None:
        """Notify async targets that this session just went fully idle.

        Best-effort: any failure inside a notifier is logged by the
        manager. Skipped if no manager is wired (tests, etc.).
        """
        if self._notifier_manager is None:
            return
        try:
            from ..notifiers import NotifierEvent

            await self._notifier_manager.fire(
                NotifierEvent(
                    type="session_idle",
                    title=session.name or "Session idle",
                    message=(
                        f"Session '{session.name}' finished its work and is idle."
                    ),
                    session_id=session.id,
                    session_name=session.name,
                )
            )
        except Exception:
            logger.exception(
                "notifier_manager.fire raised for session %s", session.id
            )




def _guess_mime(filename: str) -> str:
    """Lightweight MIME guess for replayed attachments.

    Mirrors the upload-time logic in `server.attachments._detect_mime`,
    but we don't have the client's declared MIME at replay so we always
    derive from the filename extension.
    """
    import mimetypes

    guess, _ = mimetypes.guess_type(filename)
    return guess or "application/octet-stream"


def _split_tool_list(raw: str | None) -> list[str] | None:
    """Parse an agent's newline-separated tool/MCP name list.

    Empty / whitespace-only → None (meaning "no restriction" for allow,
    "nothing extra" for deny). Order preserved, blanks dropped.
    """
    if not raw:
        return None
    items = [line.strip() for line in raw.splitlines() if line.strip()]
    return items or None


def _augment_prompt_with_attachments(prompt: str, paths: list[str]) -> str:
    """Prepend an `<attachments>` block listing absolute paths.

    The agent (Claude Code, Codex, anything with a Read tool) sees the
    paths in its input and can open them on demand. Format kept terse
    and obvious — one path per line so the model doesn't have to parse
    anything clever.
    """
    if not paths:
        return prompt
    lines = ["<attachments>"]
    lines.extend(f"- {p}" for p in paths)
    lines.append("</attachments>")
    lines.append("")
    lines.append(prompt)
    return "\n".join(lines)


# Singleton
