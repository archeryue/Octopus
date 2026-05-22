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

import json
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
from ..oauth_providers import OAuthTokenSet

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
        status=row.get("status") or "active",
        token_expires_at=row.get("token_expires_at"),
        needs_reconnect=bool(row.get("needs_reconnect", False)),
        last_refresh_error_code=row.get("last_refresh_error_code"),
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
    row = await db.get_credential(credential_id)
    deleted = await db.delete_credential(credential_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="credential not found")
    # A Codex credential is a directory-backed login — remove its CODEX_HOME
    # (auth.json + token) so deletion actually revokes local access.
    if row is not None and row.get("backend") == BackendKind.codex.value:
        import shutil

        from ..codex_login import codex_home_for

        shutil.rmtree(codex_home_for(credential_id), ignore_errors=True)


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
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return OAuthStartResponse(login_id=session.id, device_url=session.url)


def _serialize_oauth_tokens(ts: OAuthTokenSet) -> str:
    """Encode an OAuthTokenSet as the on-disk secret blob.

    Stored as JSON so the resolver can refresh the access_token in place
    without re-running the entire login flow. The shape is intentionally
    flat — no nested objects — to keep refresh writes cheap.
    """
    return json.dumps(
        {
            "access_token": ts.access_token,
            "refresh_token": ts.refresh_token,
            "expires_at_epoch": ts.expires_at_epoch,
            "scopes": list(ts.scopes),
            "token_type": ts.token_type,
        },
        separators=(",", ":"),
    )


@router.post(
    "/oauth/complete",
    response_model=CredentialInfo,
    status_code=status.HTTP_201_CREATED,
)
async def oauth_complete(
    req: OAuthCompleteRequest, _: str = Depends(verify_token)
):
    """Submit the code copied from the OAuth callback, exchange it for a
    long-lived API key via Anthropic's OAuth + api-key endpoints, and
    persist the credential.

    Two completion shapes from the orchestrator:
      - `session.token` set → Console org user with a fresh sk-ant- key.
        Stored as auth_type=oauth, no expiry tracking needed.
      - `session.oauth_tokens` set → Pro/Max subscriber whose token can't
        mint an API key. We store the full OAuthTokenSet as JSON, with
        token_expires_at populated so the resolver knows when to refresh.
    """
    db = _require_db()
    try:
        session = await oauth_login_manager.submit_code(req.login_id, req.code)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown login_id")
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if session.state != LoginState.success:
        raise HTTPException(
            status_code=500,
            detail=session.message or "login completed without a usable result",
        )

    cid = uuid.uuid4().hex[:12]
    created_at = datetime.now(timezone.utc).isoformat()

    if session.token:
        # API-key path: long-lived sk-ant- key from create_api_key endpoint.
        secret_encrypted = encrypt(session.token, settings.auth_token)
        token_expires_at: str | None = None
    elif session.oauth_tokens:
        # OAuth-token path: store the full token set; resolver refreshes.
        ts = session.oauth_tokens
        secret_encrypted = encrypt(
            _serialize_oauth_tokens(ts), settings.auth_token
        )
        token_expires_at = datetime.fromtimestamp(
            ts.expires_at_epoch, tz=timezone.utc
        ).isoformat()
    else:
        # State machine guarantees one of the two is set on success, but
        # be defensive — a third shape sneaking in would otherwise corrupt
        # the credential row silently.
        raise HTTPException(
            status_code=500,
            detail="login completed with no token or oauth_tokens",
        )

    await db.save_credential(
        credential_id=cid,
        backend=BackendKind.claude_code.value,
        label=req.label,
        auth_type=AuthType.oauth.value,
        secret_encrypted=secret_encrypted,
        created_at=created_at,
    )
    if token_expires_at is not None:
        await db.update_credential(cid, token_expires_at=token_expires_at)
    return CredentialInfo(
        id=cid,
        backend=BackendKind.claude_code,
        label=req.label,
        auth_type=AuthType.oauth,
        created_at=created_at,
        token_expires_at=token_expires_at,
    )


@router.post("/oauth/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def oauth_cancel(req: OAuthCancelRequest, _: str = Depends(verify_token)):
    """Abort an in-flight login (kills the subprocess). Idempotent."""
    await oauth_login_manager.cancel(req.login_id)


# ---------------------------------------------------------------------------
# Codex (ChatGPT) in-app device-auth login
# ---------------------------------------------------------------------------
#
# Unlike Claude (HTTP + pasted code), Codex auth is directory-backed: we run
# `codex login --device-auth` against a per-credential CODEX_HOME and surface
# the URL + one-time code. Codex polls for authorization and writes auth.json
# into that dir on success; the credential row points at the dir, no secret.


class CodexLoginStartRequest(BaseModel):
    label: str = Field(min_length=1)


class CodexLoginStartResponse(BaseModel):
    # Returns immediately after spawning — the URL + code aren't ready yet
    # (codex fetches them from auth.openai.com); the UI polls `status` for them.
    login_id: str


class CodexLoginStatusResponse(BaseModel):
    state: str  # CodexLoginState value
    verification_url: str | None = None
    user_code: str | None = None
    message: str | None = None
    credential: CredentialInfo | None = None


class CodexLoginCancelRequest(BaseModel):
    login_id: str


@router.post(
    "/codex/start",
    response_model=CodexLoginStartResponse,
    status_code=status.HTTP_201_CREATED,
)
async def codex_login_start(
    req: CodexLoginStartRequest, _: str = Depends(verify_token)
):
    from ..codex_login import codex_login_manager

    try:
        session = await codex_login_manager.start(req.label.strip())
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return CodexLoginStartResponse(login_id=session.id)


@router.get("/codex/{login_id}/status", response_model=CodexLoginStatusResponse)
async def codex_login_status(login_id: str, _: str = Depends(verify_token)):
    """Poll an in-flight Codex login. On success, persist the credential row
    (pointing at the CODEX_HOME dir) once and return it."""
    from ..codex_login import CodexLoginState, codex_login_manager

    session = codex_login_manager.get(login_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown login_id")

    credential: CredentialInfo | None = None
    if session.state == CodexLoginState.success and not session.persisted:
        db = _require_db()
        created_at = datetime.now(timezone.utc).isoformat()
        # No real secret for Codex — the credential *is* the CODEX_HOME dir.
        # Store the dir path (encrypted, harmless) so the row is self-describing;
        # session_manager resolves the dir deterministically from credential_id.
        await db.save_credential(
            credential_id=session.credential_id,
            backend=BackendKind.codex.value,
            label=session.label,
            auth_type=AuthType.oauth.value,
            secret_encrypted=encrypt(session.codex_home, settings.auth_token),
            created_at=created_at,
        )
        session.persisted = True
        credential = CredentialInfo(
            id=session.credential_id,
            backend=BackendKind.codex,
            label=session.label,
            auth_type=AuthType.oauth,
            created_at=created_at,
        )

    return CodexLoginStatusResponse(
        state=session.state.value,
        verification_url=session.verification_url,
        user_code=session.user_code,
        message=session.message,
        credential=credential,
    )


@router.post("/codex/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def codex_login_cancel(
    req: CodexLoginCancelRequest, _: str = Depends(verify_token)
):
    from ..codex_login import codex_login_manager

    await codex_login_manager.cancel(req.login_id)
