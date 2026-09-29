"""Is this request allowed in at all.

Two eras meet here (multi-tenancy.md §9). Before the first account exists, the
install is the single-user one it has always been and `OCTOPUS_AUTH_TOKEN` is
the way in. From the moment an account exists, it is a site: only a session
bearer or a personal access token opens anything, and the global token is dead.

That is not a dual path kept for convenience — it is self-limiting. It cannot
be used to bypass an account, because it stops working the instant the first
one is created, and there is no configuration that brings it back.

`verify_token` still returns the presented bearer, so the ~100 routes that
declare it did not have to change while ownership lands. Knowing *who* is
asking is `deps.current_user`; this only decides whether to let them in.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings

_bearer = HTTPBearer()


async def _allowed(presented: str) -> bool:
    from . import deps

    if presented and deps._user_manager is not None:
        if await deps._user_manager.resolve_token(presented) is not None:
            return True
    # Pre-accounts only, and never once an account exists.
    if await deps.accounts_exist():
        return False
    return bool(presented) and presented == settings.auth_token


async def verify_token(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    if not await _allowed(creds.credentials):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    return creds.credentials


async def verify_ws_token(token: str = Query(...)) -> str:
    if not await _allowed(token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    return token
