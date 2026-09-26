"""Backwards-compatible re-export of the turn engine.

The implementation moved to `server/sessions/` — one file per responsibility
rather than one 4,017-line class (docs/plans/polish-2026-09.md §3 A1). This
module stays because 18 modules import `session_manager` from here, and the
split was meant to make the code readable, not to make every caller churn.
"""

from __future__ import annotations

from .sessions import (
    _DELTA_FLUSH_SECONDS,
    _HELD_PROCESS_IDLE_SECONDS,
    _HELD_STOP_TIMEOUT,
    _MAX_HELD_PROCESSES,
    _MAX_PENDING_STEERS,
    _MAX_SUBAGENTS_PER_SESSION,
    _REAPER_INTERVAL_SECONDS,
    ForkError,
    PendingApproval,
    PendingQuestion,
    QueuedPrompt,
    Session,
    SessionManager,
    fork_info_fields,
    resolve_working_dir,
    session_manager,
)

# The tuning constants, re-exported because tests read them to assert against
# the real bound rather than a copy of the number — which is the right way to
# write those tests, and means this module is part of their contract too.
__all__ = [
    "_DELTA_FLUSH_SECONDS",
    "_HELD_PROCESS_IDLE_SECONDS",
    "_HELD_STOP_TIMEOUT",
    "_MAX_HELD_PROCESSES",
    "_MAX_PENDING_STEERS",
    "_MAX_SUBAGENTS_PER_SESSION",
    "_REAPER_INTERVAL_SECONDS",

    "ForkError",
    "PendingApproval",
    "PendingQuestion",
    "QueuedPrompt",
    "Session",
    "SessionManager",
    "fork_info_fields",
    "resolve_working_dir",
    "session_manager",
]
