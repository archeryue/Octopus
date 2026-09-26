"""The persistence layer.

`Database` was a single 2,600-line class with 94 methods covering ten
unrelated subjects — sessions, agents, schedules, credentials, connectors,
background tasks, research jobs, applications, notifiers, and its own
connection lifecycle. It is now one file per subject
(docs/plans/polish-2026-09.md §3 A4).

**Composition, not decomposition of the interface.** `Database` still presents
every method as one object, so not a single call site changed — `db.save_session`
is still `db.save_session`. A repository-object split (`db.sessions.save(...)`)
would have touched 200+ call sites to buy the same file-level separation, and
churn on that scale is where regressions come from.

Each mixin inherits `DatabaseBase` rather than declaring the attributes it
borrows, so the type checker knows exactly what a mixin may touch and a typo
is an error rather than an AttributeError at runtime.
"""

from __future__ import annotations

from .agents import AgentsMixin
from .applications import ApplicationsMixin
from .base import DatabaseBase
from .bg_tasks import BgTasksMixin
from .connectors import ConnectorsMixin
from .credentials import CredentialsMixin
from .notifiers import NotifiersMixin
from .research import ResearchMixin
from .schedules import SchedulesMixin
from .sessions import SessionsMixin


class Database(
    SessionsMixin,
    AgentsMixin,
    SchedulesMixin,
    CredentialsMixin,
    ConnectorsMixin,
    BgTasksMixin,
    ResearchMixin,
    ApplicationsMixin,
    NotifiersMixin,
    DatabaseBase,
):
    """SQLite persistence (aiosqlite, WAL, FK cascade).

    The mixins are listed in the order a reader is likely to want them, and
    `DatabaseBase` last so it sits at the end of the MRO — every mixin already
    inherits it, so this only fixes the order, it does not add a base.
    """


__all__ = ["Database", "DatabaseBase"]
