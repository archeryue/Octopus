"""Request-scoped dependencies.

`session_manager = SessionManager()` is created at import and was imported
directly by 18 modules, which meant test isolation depended on resetting a
global by hand and the object could not be instantiated twice in one process
(docs/plans/polish-2026-09.md §3 A2).

The singleton stays — every background path (the scheduler, the delegation
manager, the bridges) genuinely wants the one live instance, and removing it
would churn the whole codebase for nothing. What changes is how a *request*
reaches it: routers declare `session_manager: SessionMgr` and FastAPI resolves
it, so a test can substitute its own with `app.dependency_overrides` instead of
monkeypatching a module attribute in each router, and nothing in the request
path is bound to a particular instance any more.

The parameter is deliberately named `session_manager`, the same as the global it
replaces: the name was already right, and reusing it means no call site inside
a route had to change — the same argument the `Database` and `SessionManager`
splits made for keeping one object with the same method names.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from .sessions import SessionManager
from .sessions import session_manager as _singleton


def get_session_manager() -> SessionManager:
    """The session manager this request should use."""
    return _singleton


# What a router annotates a parameter with. `Annotated` rather than a default,
# so the parameter has no default value and can sit first in a signature
# regardless of what follows it.
SessionMgr = Annotated[SessionManager, Depends(get_session_manager)]
