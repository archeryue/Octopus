"""Turning a failed turn into something the user can act on.

One slice of `SessionManager`, which was a single 4,017-line class with 84
methods (docs/plans/polish-2026-09.md §3 A1). Split by responsibility; the
methods are unchanged and still reached through one `session_manager` object,
so no call site moved.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..config import settings
from ..harness import HarnessRun
from ..models import MessageContent, MessageRole
from ..oauth_errors import RefreshErrorCode
from .base import (
    Session,
    SessionManagerBase,
    logger,
)


class OutcomesMixin(SessionManagerBase):

    def _start_turn_watchdog(
        self, backend: HarnessRun, state: dict[str, Any]
    ) -> asyncio.Task | None:
        """Stop `backend` if the turn goes silent for `turn_idle_timeout_seconds`
        or runs past `turn_max_seconds` (turn-safety.md §3). Returns the watchdog
        task (or None if both checks are disabled). On a trip it records
        `state["tripped"] = (reason, limit)` and calls `backend.stop()`, which
        emits the stream-end sentinel so the run loop unblocks cleanly — no
        generator cancellation. `state["last"]` is the caller-updated last-event
        timestamp."""
        idle = settings.turn_idle_timeout_seconds
        overall = settings.turn_max_seconds
        if idle <= 0 and overall <= 0:
            return None
        limits = [x for x in (idle, overall) if x and x > 0]
        tick = min(5.0, max(0.05, min(limits) / 4))
        started = time.monotonic()

        async def _run() -> None:
            while True:
                await asyncio.sleep(tick)
                now = time.monotonic()
                if idle > 0 and now - state["last"] > idle:
                    state["tripped"] = ("idle", idle)
                elif overall > 0 and now - started > overall:
                    state["tripped"] = ("overall", overall)
                else:
                    continue
                logger.warning(
                    "Session turn watchdog tripped (%s); stopping backend",
                    state["tripped"][0],
                )
                try:
                    await backend.stop()
                except Exception:
                    logger.exception("watchdog backend.stop() failed")
                return

        return asyncio.create_task(_run())

    async def _surface_turn_timeout(
        self, session: Session, *, reason: str, limit: int, backend: str
    ) -> dict[str, Any]:
        """Persist + return the error for a watchdog-stopped turn
        (turn-safety.md §3)."""
        display = self._BACKEND_DISPLAY.get(backend, backend)
        if reason == "idle":
            human = (
                f"This turn was stopped after {limit}s with no activity from "
                f"{display} — it looked wedged (e.g. a tool or sub-task that "
                "never returned). Nothing was lost; try again, and consider "
                "narrowing the task."
            )
        else:
            human = (
                f"This turn hit the {limit}s maximum duration and was stopped. "
                "Try again or break the work into smaller steps."
            )
        seq = await self._persist_message(
            session,
            MessageContent(
                role=MessageRole.system, type="error", content=human, is_error=True
            ),
        )
        event: dict[str, Any] = {
            "type": "error",
            "session_id": session.id,
            "message": human,
            "code": "turn_timeout",
        }
        if seq is not None:
            event["seq"] = seq
        return event

    async def _surface_auth_expiry(
        self, session: Session, *, cred_id: str | None, backend: str
    ) -> dict[str, Any]:
        """Flag the bound credential needs_reconnect (if any) and build the
        re-authorize chat event for a mid-turn 401 (harness-credential-reauth.md
        §4). Persists a human-readable system error so it survives reload; the
        returned WS event additionally carries `code`/`credential_id`/`backend`
        so the client can refresh the sidebar and light up the re-auth badge.
        """
        label: str | None = None
        if cred_id is not None:
            await self._mark_needs_reconnect(
                cred_id, RefreshErrorCode.invalid_credentials
            )
            if self.db:
                row = await self.db.get_credential(cred_id)
                if row:
                    label = row.get("label")
        display = self._BACKEND_DISPLAY.get(backend, backend)
        if cred_id is not None:
            quoted = f" “{label}”" if label else ""
            human = (
                f"Authentication failed (401) for the {display} credential"
                f"{quoted}. Its sign-in has expired or been revoked — "
                "re-authorize it in the Harness section of the sidebar, then "
                "resend your message."
            )
        else:
            human = (
                f"Authentication failed (401) for {display}. The CLI's own "
                "sign-in has expired — re-authorize the backend, then resend "
                "your message."
            )
        seq = await self._persist_message(
            session,
            MessageContent(
                role=MessageRole.system, type="error", content=human, is_error=True
            ),
        )
        event: dict[str, Any] = {
            "type": "error",
            "session_id": session.id,
            "message": human,
            "code": "auth_expired",
            "credential_id": cred_id,
            "backend": backend,
        }
        if seq is not None:
            event["seq"] = seq
        return event

    async def _surface_stale_session(
        self, session: Session, *, backend: str, replayed: int = 0, omitted: int = 0
    ) -> dict[str, Any]:
        """Persist + return the marker for a dropped resume id.

        Worth saying out loud rather than hiding: the engine lost its copy of
        the conversation, and what it sees this turn is a replay of ours —
        recent messages only, and tool results as summaries rather than live
        state. Silently continuing would leave the user guessing why the
        agent's recall suddenly got shallower."""
        if replayed:
            human = (
                f"({backend} lost this conversation's history — replaying the "
                f"last {replayed} message(s) from Octopus so it can continue"
            )
            human += f"; {omitted} earlier one(s) omitted)" if omitted else ")"
        else:
            human = (
                f"({backend} lost this conversation's history and there was "
                f"nothing to replay — starting a fresh engine session)"
            )
        seq = await self._persist_message(
            session,
            MessageContent(role=MessageRole.system, type="error", content=human),
        )
        event: dict[str, Any] = {
            "type": "error",
            "session_id": session.id,
            "message": human,
            "code": "stale_session",
        }
        if seq is not None:
            event["seq"] = seq
        return event

    async def _surface_transient_retry(
        self, session: Session, *, attempt: int, max_attempts: int, delay: float
    ) -> dict[str, Any]:
        """Persist + return a discreet marker that the turn hit a transient
        backend error and is being retried (harness-transient-retry.md §4), so
        the delay isn't a silent stall. Mirrors the premature-exit marker."""
        human = (
            f"(transient backend error — retrying in {delay:.0f}s, "
            f"attempt {attempt}/{max_attempts})"
        )
        seq = await self._persist_message(
            session,
            MessageContent(role=MessageRole.system, type="error", content=human),
        )
        event: dict[str, Any] = {
            "type": "error",
            "session_id": session.id,
            "message": human,
            "code": "transient_retry",
        }
        if seq is not None:
            event["seq"] = seq
        return event

    async def _surface_transient_exhausted(
        self, session: Session, *, backend: str, attempts: int
    ) -> dict[str, Any]:
        """Persist + return a clear error once transient retries are exhausted,
        so a provider-side blip doesn't read as a silent failure of the user's
        request (harness-transient-retry.md §4)."""
        display = self._BACKEND_DISPLAY.get(backend, backend)
        if attempts > 0:
            tail = (
                f"kept failing with a transient error after {attempts} "
                f"{'retry' if attempts == 1 else 'retries'}"
            )
        else:
            tail = "hit a transient error mid-turn and couldn't be safely resumed"
        human = (
            f"The {display} backend {tail}. This is a provider-side issue, not "
            "your request — please try again in a moment."
        )
        seq = await self._persist_message(
            session,
            MessageContent(
                role=MessageRole.system, type="error", content=human, is_error=True
            ),
        )
        event: dict[str, Any] = {
            "type": "error",
            "session_id": session.id,
            "message": human,
            "code": "transient_exhausted",
        }
        if seq is not None:
            event["seq"] = seq
        return event
