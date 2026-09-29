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

from typing import Annotated, Any

from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .sessions import SessionManager
from .sessions import session_manager as _singleton
from .users import UserManager


def get_session_manager() -> SessionManager:
    """The session manager this request should use."""
    return _singleton


# What a router annotates a parameter with. `Annotated` rather than a default,
# so the parameter has no default value and can sit first in a signature
# regardless of what follows it.
SessionMgr = Annotated[SessionManager, Depends(get_session_manager)]


# ---------------------------------------------------------------------------
# Who is asking (multi-tenancy.md §5.1)
# ---------------------------------------------------------------------------
#
# There are ~100 routes. Writing `WHERE user_id = ?` into each of them is how
# multi-tenant systems leak: it only has to be forgotten once, by anyone, ever,
# and the bug is invisible until someone sees another account's data.
#
# So the scoping lives here instead. A route asks for `CurrentUser` and gets an
# authenticated account or a 401; it asks for `Ctx` and gets that account
# *together with* the managers already bound to it. A route that wants another
# user's rows has to go out of its way rather than merely forget something.

_user_manager: UserManager | None = None


def set_user_manager(manager: UserManager) -> None:
    """Bound at startup, like the other managers (`main.py`)."""
    global _user_manager
    _user_manager = manager


def get_user_manager() -> UserManager:
    if _user_manager is None:  # pragma: no cover - startup wiring bug
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "accounts are not available"
        )
    return _user_manager


UserMgr = Annotated[UserManager, Depends(get_user_manager)]

_bearer = HTTPBearer(auto_error=False)

# Annotated rather than `= Depends(...)` defaults, which is the style this
# module already states in its docstring — and the reason it is not on the
# B008 ignore list the routers are on.
_BearerCreds = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
_QueryToken = Annotated[str | None, Query()]


def _presented(
    request: Request,
    creds: HTTPAuthorizationCredentials | None,
    query_token: str | None,
) -> str:
    """The bearer this request carries, from any of the three places one can
    travel: the header, `?token=` (a WebSocket cannot set headers) and the
    application cookie (neither can an iframe)."""
    if creds is not None and creds.credentials:
        return creds.credentials
    if query_token:
        return query_token
    return request.cookies.get("octopus_app_token") or ""


async def current_user(
    request: Request,
    manager: UserMgr,
    creds: _BearerCreds,
    token: _QueryToken = None,
) -> dict[str, Any]:
    """The account this request belongs to, or 401.

    One failure for every way a bearer can be no good — unknown, revoked,
    expired, belonging to a disabled account — because a reply that
    distinguished them would tell an attacker which.
    """
    user = await manager.resolve_token(_presented(request, creds, token))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    return user


CurrentUser = Annotated[dict, Depends(current_user)]


async def require_admin(user: CurrentUser) -> dict[str, Any]:
    if not user.get("is_admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admins only")
    return user


AdminUser = Annotated[dict, Depends(require_admin)]
