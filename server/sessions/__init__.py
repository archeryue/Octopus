"""The turn engine.

`SessionManager` was one 4,017-line class with 84 methods carrying at least
eight separable responsibilities — driving a turn, forking, session lifecycle,
credential resolution, the held-process pool, questions and approvals, failure
presentation, and event conversion. It is now one file per responsibility
(docs/plans/polish-2026-09.md §3 A1).

Composed the same way the database was (§3 A4): every method still hangs off
one object, so nothing that calls `session_manager.send_message(...)` changed.
The module-level `session_manager` singleton is still exported from
`server.session_manager`, which is where 18 modules import it from.
"""

from __future__ import annotations

from .base import (
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
    SessionManagerBase,
    fork_info_fields,
    resolve_working_dir,
)
from .credentials import CredentialsMixin
from .events import EventsMixin
from .forking import ForkingMixin
from .lifecycle import LifecycleMixin
from .outcomes import OutcomesMixin
from .processes import ProcessesMixin
from .questions import QuestionsMixin
from .turns import TurnsMixin


class SessionManager(
    TurnsMixin,
    ForkingMixin,
    LifecycleMixin,
    CredentialsMixin,
    ProcessesMixin,
    QuestionsMixin,
    OutcomesMixin,
    EventsMixin,
    SessionManagerBase,
):
    """Owns every live `Session` and drives each turn through the Harness.

    Mixins are ordered by how central they are to reading the class, with the
    base last so it sits at the end of the MRO.
    """


# One process-wide instance. Imported directly by 18 modules, so it stays a
# module-level singleton rather than becoming a factory — that change belongs
# with A2, which makes routers take it as a dependency.
session_manager = SessionManager()


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
