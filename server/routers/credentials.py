"""REST endpoints for per-backend credentials.

Secrets are encrypted at rest with the auth token as the key (see
`server/crypto.py`). The wire format never includes the plaintext secret —
the only way to use a credential is to attach it to a session that the
backend then resolves at run time.

Two acquisition paths:
  - POST /api/credentials                — paste an API key (legacy/manual)
  - POST /api/credentials/oauth/start    — sign in via Claude OAuth
    POST /api/credentials/oauth/complete — submit code, store issued token
    POST /api/credentials/oauth/cancel   — abort an in-flight login

The OAuth flow yields a long-lived `sk-ant-…` token, which is stored the
same way as a manually-pasted API key.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..auth import verify_token
from ..config import settings
from ..crypto import encrypt
from ..models import (
    AuthType,
    BackendKind,
    CreateCredentialRequest,
    CredentialInfo,
    UpdateCredentialRequest,
)
from ..oauth_login import LoginState, oauth_login_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/credentials", tags=["credentials"])

# Set at app startup. Module-level lets the router stay light without DI.
_db = None


def set_db(db) -> None:
    global _db
    _db = db


def _require_db():
    if _db is None:
        raise HTTPException(
            status_code=503, detail="credential database not yet initialized"
        )
    return _db


def _row_to_info(row: dict) -> CredentialInfo:
    return CredentialInfo(
        id=row["id"],
        backend=row["backend"],
        label=row["label"],
        auth_type=row["auth_type"],
        created_at=row["created_at"],
    )


@router.get("", response_model=list[CredentialInfo])
async def list_credentials(_: str = Depends(verify_token)):
    rows = await _require_db().load_credentials()
    return [_row_to_info(r) for r in rows]


@router.post("", response_model=CredentialInfo, status_code=status.HTTP_201_CREATED)
async def create_credential(
    req: CreateCredentialRequest, _: str = Depends(verify_token)
):
    db = _require_db()
    cid = uuid.uuid4().hex[:12]
    created_at = datetime.now(timezone.utc).isoformat()
    secret_encrypted = encrypt(req.secret, settings.auth_token)
    await db.save_credential(
        credential_id=cid,
        backend=req.backend.value,
        label=req.label,
        auth_type=req.auth_type.value,
        secret_encrypted=secret_encrypted,
        created_at=created_at,
    )
    return CredentialInfo(
        id=cid,
        backend=req.backend,
        label=req.label,
        auth_type=req.auth_type,
        created_at=created_at,
    )


@router.patch("/{credential_id}", response_model=CredentialInfo)
async def update_credential(
    credential_id: str,
    req: UpdateCredentialRequest,
    _: str = Depends(verify_token),
):
    db = _require_db()
    existing = await db.get_credential(credential_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="credential not found")

    update_kwargs: dict = {}
    if req.label is not None:
        update_kwargs["label"] = req.label
    if req.secret is not None:
        update_kwargs["secret_encrypted"] = encrypt(req.secret, settings.auth_token)

    if update_kwargs:
        await db.update_credential(credential_id, **update_kwargs)

    updated = await db.get_credential(credential_id)
    assert updated is not None
    return _row_to_info(updated)


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(credential_id: str, _: str = Depends(verify_token)):
    db = _require_db()
    deleted = await db.delete_credential(credential_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="credential not found")


# ---------------------------------------------------------------------------
# OAuth (in-app subscription login)
# ---------------------------------------------------------------------------


class OAuthStartRequest(BaseModel):
    # We only support claude-code for now (codex backend itself isn't
    # wired yet). The field lets the API stay forward-compatible.
    backend: BackendKind = BackendKind.claude_code


class OAuthStartResponse(BaseModel):
    login_id: str
    device_url: str


class OAuthCompleteRequest(BaseModel):
    login_id: str
    code: str = Field(min_length=1)
    label: str = Field(min_length=1)


class OAuthCancelRequest(BaseModel):
    login_id: str


@router.post(
    "/oauth/start",
    response_model=OAuthStartResponse,
    status_code=status.HTTP_201_CREATED,
)
async def oauth_start(req: OAuthStartRequest, _: str = Depends(verify_token)):
    if req.backend != BackendKind.claude_code:
        raise HTTPException(
            status_code=400,
            detail="OAuth login is only implemented for claude-code right now",
        )
    try:
        session = await oauth_login_manager.start()
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return OAuthStartResponse(login_id=session.id, device_url=session.url or "")


@router.post(
    "/oauth/complete",
    response_model=CredentialInfo,
    status_code=status.HTTP_201_CREATED,
)
async def oauth_complete(
    req: OAuthCompleteRequest, _: str = Depends(verify_token)
):
    """Submit the code copied from the OAuth callback, exchange it for a
    token via the running CLI subprocess, and persist the credential."""
    db = _require_db()
    try:
        session = await oauth_login_manager.submit_code(req.login_id, req.code)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown login_id")
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if session.state != LoginState.success or not session.token:
        raise HTTPException(
            status_code=500,
            detail=session.message or "login completed without a token",
        )

    cid = uuid.uuid4().hex[:12]
    created_at = datetime.now(timezone.utc).isoformat()
    secret_encrypted = encrypt(session.token, settings.auth_token)
    await db.save_credential(
        credential_id=cid,
        backend=BackendKind.claude_code.value,
        label=req.label,
        auth_type=AuthType.oauth.value,
        secret_encrypted=secret_encrypted,
        created_at=created_at,
    )
    return CredentialInfo(
        id=cid,
        backend=BackendKind.claude_code,
        label=req.label,
        auth_type=AuthType.oauth,
        created_at=created_at,
    )


@router.post("/oauth/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def oauth_cancel(req: OAuthCancelRequest, _: str = Depends(verify_token)):
    """Abort an in-flight login (kills the subprocess). Idempotent."""
    await oauth_login_manager.cancel(req.login_id)
