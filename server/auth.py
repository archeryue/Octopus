from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings

_bearer = HTTPBearer()


async def verify_token(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    if creds.credentials != settings.auth_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    return creds.credentials


async def verify_ws_token(token: str = Query(...)) -> str:
    if token != settings.auth_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    return token
