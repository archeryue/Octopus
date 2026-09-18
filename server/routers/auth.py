"""Access-token rotation (docs/plans/token-rotation.md).

One route, because rotating the token is one operation: the secrets in the
database are encrypted with a key derived from it, so re-keying them, changing
what the server checks and changing what clients send all have to happen
together or not at all.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from ..auth import verify_token
from ..models import TokenRotateRequest, TokenRotateResponse
from ..session_manager import session_manager
from ..token_rotation import TokenRotationError, rotate_auth_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

# Injected at startup, like the other routers' managers.
_db = None


def set_db(db) -> None:
    global _db
    _db = db


@router.post("/rotate", response_model=TokenRotateResponse)
async def rotate_token(req: TokenRotateRequest, _: str = Depends(verify_token)):
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
