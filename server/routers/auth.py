"""The access token: who holds it, and changing it.

Rotation is one route because it is one operation (docs/plans/token-rotation.md):
the secrets in the database are encrypted with a key derived from the token, so
re-keying them, changing what the server checks and changing what clients send
all have to happen together or not at all.

`GET /identity` is here for the same reason it exists at all — the sidebar used
to render the token as the account handle, so the credential was on screen for
anyone who could see the screen. It answers with a label instead.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth import verify_token
from ..bootstrap import BootstrapError, bootstrap_first_account
from ..deps import (
    AdminUser,
    CurrentUser,
    OperatorUser,
    ScopeUser,
    SessionMgr,
    UserMgr,
)
from ..models import (
    AuthStateResponse,
    BootstrapRequest,
    BootstrapResponse,
    ExtraRootsRequest,
    IdentityResponse,
    InviteCreateRequest,
    InviteInfo,
    LoginRequest,
    LoginResponse,
    PasswordChangeRequest,
    RegisterRequest,
    TokenRotateRequest,
    TokenRotateResponse,
    UserDisabledRequest,
    UserInfo,
    WorkspaceInfo,
)
from ..token_rotation import TokenRotationError, rotate_auth_token
from ..users import UserError, token_digest
from ..workspace import normalise_root, paths_for

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

# Injected at startup, like the other routers' managers.
_db = None


def set_db(db) -> None:
    global _db
    _db = db


def _throttle_key(username: str, request: Request) -> str:
    client = request.client.host if request.client else "?"
    return f"{username.strip().lower()}@{client}"


# Failed logins, per username-and-address. In memory on purpose: a password is
# guessable in a way the old 256-bit token never was, so the login route needs
# a cost that rises — and a counter that resets when the server restarts is
# both enough against online guessing and one fewer table to reason about. It
# is not a defence against a distributed attempt, which is what a rate limit in
# front of the process is for.
_FAILURES: dict[str, list[float]] = {}
_WINDOW_SECONDS = 300.0
_MAX_FAILURES = 8


def _too_many_failures(key: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _FAILURES.get(key, []) if now - t < _WINDOW_SECONDS]
    _FAILURES[key] = recent
    return len(recent) >= _MAX_FAILURES


def _record_failure(key: str) -> None:
    _FAILURES.setdefault(key, []).append(time.monotonic())


@router.get("/state", response_model=AuthStateResponse)
async def auth_state() -> AuthStateResponse:
    """Whether this install has accounts yet.

    Unauthenticated, deliberately and narrowly: the sign-in screen cannot ask
    the right question without it, and the answer — "has anybody set this box
    up" — is already implied by whether the sign-in screen works at all.
    """
    from .. import deps

    return AuthStateResponse(accounts_exist=await deps.accounts_exist())


@router.post("/bootstrap", response_model=BootstrapResponse)
async def bootstrap(
    req: BootstrapRequest,
    users: UserMgr,
    session_manager: SessionMgr,
    _: str = Depends(verify_token),
) -> BootstrapResponse:
    """Create this install's first account, and hand it what is already here.

    Authenticated with the install's own token, which is the only credential
    that exists at this point and stops working the moment this succeeds — the
    two facts are the same fact (§9). Refused outright once an account exists,
    so it cannot be a second way in.
    """
    if _db is None:
        raise HTTPException(503, "database not available")
    try:
        summary = await bootstrap_first_account(
            _db,
            username=req.username,
            password=req.password,
            session_manager=session_manager,
        )
    except BootstrapError as e:
        raise HTTPException(e.status_code, e.message) from e

    token = await users.issue_token(str(summary["user_id"]))
    return BootstrapResponse(
        token=token,
        user_id=str(summary["user_id"]),
        username=str(summary["username"]),
        summary=summary,
    )


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest, request: Request, users: UserMgr) -> LoginResponse:
    """Sign in and receive a session bearer.

    The password is never a bearer: it is spent here, once, for a token that
    can be revoked without changing it.
    """
    key = _throttle_key(req.username, request)
    if _too_many_failures(key):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many attempts — wait a few minutes and try again",
        )
    try:
        user = await users.authenticate(req.username, req.password)
    except UserError as e:
        _record_failure(key)
        raise HTTPException(e.status_code, e.message) from e

    _FAILURES.pop(key, None)
    token = await users.issue_token(user["id"])
    logger.info("login: %s", user["username"])
    return LoginResponse(
        token=token,
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
    )


@router.post("/register", response_model=LoginResponse)
async def register(req: RegisterRequest, users: UserMgr) -> LoginResponse:
    """Create an account against an invite code, and sign it in.

    Signed in on success because the alternative is asking someone to type the
    password they just chose, which teaches nothing and only loses people.
    """
    try:
        user = await users.register(
            invite_code=req.invite_code,
            username=req.username,
            password=req.password,
        )
    except UserError as e:
        raise HTTPException(e.status_code, e.message) from e

    token = await users.issue_token(user["id"])
    logger.info("registered: %s", user["username"])
    return LoginResponse(
        token=token,
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, users: UserMgr, _user: CurrentUser) -> None:
    """Revoke the bearer this request came with — this device only."""
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        await users.revoke_token(auth[7:].strip())


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    req: PasswordChangeRequest, request: Request, users: UserMgr, user: CurrentUser
) -> None:
    """Change your own password, which signs your other devices out.

    The current password is required even though the request is already
    authenticated: a bearer left behind on a shared machine must not be enough
    to take the account over.
    """
    try:
        await users.authenticate(user["username"], req.current_password)
    except UserError as e:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Current password is wrong"
        ) from e

    auth = request.headers.get("authorization") or ""
    keep = token_digest(auth[7:].strip()) if auth.lower().startswith("bearer ") else None
    try:
        await users.set_password(user["id"], req.new_password, keep_token_hash=keep)
    except UserError as e:
        raise HTTPException(e.status_code, e.message) from e


@router.get("/identity", response_model=IdentityResponse)
async def identity(user: CurrentUser) -> IdentityResponse:
    """Who you are, for the sidebar's account row.

    It used to answer `OCTOPUS_USER_LABEL` — a single-user install's idea of a
    name. Now it answers the account's own username, which is the same field
    doing the same job for a site with several people on it.
    """
    return IdentityResponse(
        label=user["username"], user_id=user["id"], is_admin=user["is_admin"]
    )


@router.get("/workspace", response_model=WorkspaceInfo)
async def workspace(users: UserMgr, user_id: ScopeUser = None) -> WorkspaceInfo:
    """Where this account may work.

    The UI needs it to explain a refusal: "that path is outside your workspace"
    is only actionable if you can see what the workspace *is* and what else has
    been opened up.
    """
    if user_id is None:
        # No accounts: nothing has ever been confined, and saying otherwise
        # would invite somebody to fix a restriction that is not there.
        return WorkspaceInfo(
            workspace=str(paths_for(None).workspace), extra_roots=[], confined=False
        )
    row = await users.db.get_user(user_id)
    raw = (row or {}).get("extra_roots")
    try:
        roots = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        roots = []
    return WorkspaceInfo(
        workspace=str(paths_for(user_id).workspace),
        extra_roots=[str(r) for r in roots],
        confined=True,
    )


@router.put("/users/{user_id}/extra-roots", response_model=WorkspaceInfo)
async def set_extra_roots(
    user_id: str,
    req: ExtraRootsRequest,
    users: UserMgr,
    _operator: OperatorUser = None,
) -> WorkspaceInfo:
    """Open a path outside an account's workspace to it, or close one.

    The operator's, not the account holder's, and deliberately: an extra root
    is a hole in the confinement, so an account that could punch its own would
    make the confinement advisory. On an install with one person that is the
    same person — which is the point of the affordance (§6), not a hole in it.

    The list is replaced whole, and every entry is vetted by `normalise_root`;
    what is stored is the resolved path, so a symlink cannot change what an
    approved root means afterwards.
    """
    if await users.db.get_user(user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such account")
    roots = [str(normalise_root(r)) for r in req.extra_roots]
    # Deduplicated, keeping the order given, so the list reads as it was typed.
    seen: dict[str, None] = {}
    for r in roots:
        seen.setdefault(r, None)
    await users.db.update_user_field(user_id, extra_roots=json.dumps(list(seen)))
    return WorkspaceInfo(
        workspace=str(paths_for(user_id).workspace),
        extra_roots=list(seen),
        confined=True,
    )


# ---------------------------------------------------------------------------
# Admin: who may join, and who may stay
# ---------------------------------------------------------------------------


@router.post("/invites", response_model=InviteInfo)
async def create_invite(
    req: InviteCreateRequest, users: UserMgr, admin: AdminUser
) -> InviteInfo:
    try:
        invite = await users.create_invite(
            created_by=admin["id"], max_uses=req.max_uses, ttl_days=req.ttl_days
        )
    except UserError as e:
        raise HTTPException(e.status_code, e.message) from e
    return InviteInfo(**invite)


@router.get("/invites", response_model=list[InviteInfo])
async def list_invites(users: UserMgr, _admin: AdminUser) -> list[InviteInfo]:
    return [InviteInfo(**row) for row in await users.db.list_invites()]


@router.delete("/invites/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(code: str, users: UserMgr, _admin: AdminUser) -> None:
    await users.db.revoke_invite(code, at=datetime.now(UTC).isoformat())


@router.get("/users", response_model=list[UserInfo])
async def list_users(users: UserMgr, _admin: AdminUser) -> list[UserInfo]:
    return [UserInfo(**row) for row in await users.db.list_users()]


@router.post("/users/{user_id}/disabled", response_model=UserInfo)
async def set_user_disabled(
    user_id: str, req: UserDisabledRequest, users: UserMgr, admin: AdminUser
) -> UserInfo:
    """Disable or restore an account.

    An admin cannot disable themselves: the one way to end up with a site
    nobody can administer is to allow it.
    """
    if user_id == admin["id"]:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "You cannot disable your own account"
        )
    try:
        await users.set_disabled(user_id, req.disabled)
    except UserError as e:
        raise HTTPException(e.status_code, e.message) from e
    row = await users.db.get_user(user_id)
    assert row is not None
    return UserInfo(**row)


@router.post("/rotate", response_model=TokenRotateResponse)
async def rotate_token(session_manager: SessionMgr, req: TokenRotateRequest, _: str = Depends(verify_token)):
    """Change the access token everywhere it lives.

    Authenticated with the **old** token — which is exactly who is allowed to
    do this — and, unless `revoke_other_clients` is set, the new token is
    broadcast to clients already holding the old one so open tabs carry on
    without a re-login (§3).
    """
    if _db is None:
        raise HTTPException(503, "database not available")
    try:
        result = await rotate_auth_token(
            _db, req.new_token, session_mgr=session_manager
        )
    except TokenRotationError as e:
        raise HTTPException(e.status_code, e.message)

    await session_manager._broadcast(
        {
            "type": "auth_token_rotated",
            # Omitted when the point of the rotation is that the old token
            # leaked: then every other client must prove it has the new one.
            "token": None if req.revoke_other_clients else req.new_token.strip(),
        }
    )
    return TokenRotateResponse(
        env_files=result.env_files, reencrypted=result.reencrypted
    )
