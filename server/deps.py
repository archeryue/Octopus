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

_accounts_exist: bool | None = None


async def accounts_exist() -> bool:
    """Whether this install has any account at all.

    The answer is the difference between a site and a single-user install, and
    it is what lets the legacy `OCTOPUS_AUTH_TOKEN` keep working right up until
    the moment the first account is created and not one request after
    (multi-tenancy.md §9). Cached because it is consulted on every
    unauthenticated request and the answer only ever goes False → True.
    """
    global _accounts_exist
    if _accounts_exist:
        return True
    if _user_manager is None:
        return False
    _accounts_exist = await _user_manager.db.count_users() > 0
    return _accounts_exist


def forget_accounts_exist() -> None:
    """Drop the cached answer. Called when an account is created, and by tests
    that build a fresh database under the same process."""
    global _accounts_exist
    _accounts_exist = None


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


async def scope_user_id(
    request: Request,
    creds: _BearerCreds,
    token: _QueryToken = None,
) -> str | None:
    """Whose rows this request may see, or None when nobody owns anything yet.

    `None` is not "unscoped because we could not tell" — `verify_token` has
    already refused anyone who should not be here. It is the pre-accounts era
    (§9), where the install has one operator and no `user_id` on any row, and
    filtering by an owner that does not exist would return nothing at all.

    The moment the first account exists this always resolves to that account,
    and from then on a route filters whether or not its author thought about
    it. That is the point: ownership is the default, not a thing to remember.
    """
    if _user_manager is None:
        # No account layer bound at all. Asking for a scope must not be how a
        # request discovers that: `get_user_manager` answers 503 because a
        # *login* without accounts is broken, while a *read* without accounts
        # is the pre-accounts install working exactly as it always did.
        return None
    user = await _user_manager.resolve_token(_presented(request, creds, token))
    return user["id"] if user else None


ScopeUser = Annotated[str | None, Depends(scope_user_id)]


async def require_admin(user: CurrentUser) -> dict[str, Any]:
    if not user.get("is_admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admins only")
    return user


AdminUser = Annotated[dict, Depends(require_admin)]
