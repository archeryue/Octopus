"""In-app file viewer endpoint.

Streams a file from the session's working_dir to the browser modal.
Paired with the mcp__viewer__show_file tool (server/mcp_servers/viewer.py)
and the FileViewerDialog React component — both call here to fetch
bytes. All security checks live in server/file_viewer.py so the two
entry points (this endpoint and the MCP tool) can't drift.

Auth: bearer header OR `?token=`. The query-param path exists because
the dialog renders images/PDFs via `<img src>` / `<iframe src>`, and
those can't carry custom headers. Same auth value as the bearer one;
no second weaker token to leak.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse

from ..config import settings
from ..file_viewer import (
    FileNotFound,
    FileTooLarge,
    PathRejected,
    ResolvedFile,
    UnsupportedType,
    resolve_safe_path,
)
from ..session_manager import session_manager

router = APIRouter(prefix="/api/sessions", tags=["files"])


def _verify_token(
    request: Request, token: str | None = Query(default=None)
) -> str:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        candidate = auth_header.split(" ", 1)[1].strip()
        if candidate == settings.auth_token:
            return candidate
    if token and token == settings.auth_token:
        return token
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")


async def _working_dir_for(session_id: str) -> str:
    """Pull working_dir from the live session or, if archived, from the DB."""
    live = session_manager.get_session(session_id)
    if live is not None:
        return live.working_dir
    archived = await session_manager.load_archived_session_detail(session_id)
    if archived is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return archived.working_dir


def _resolve_or_raise(working_dir: str, path: str) -> ResolvedFile:
    try:
        return resolve_safe_path(working_dir, path)
    except PathRejected as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
    except FileNotFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    except FileTooLarge as e:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(e))
    except UnsupportedType as e:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(e))


@router.get("/{session_id}/files/meta")
async def file_meta(
    session_id: str,
    path: str = Query(...),
    _: str = Depends(_verify_token),
) -> JSONResponse:
    """Metadata-only sibling of /files.

    The dialog hits this first to decide which renderer to mount and
    whether to skip the bytes fetch entirely (e.g. for an oversized
    file, we show the error inline rather than streaming kilobytes
    that will never render). Same security path as /files.
    """
    working_dir = await _working_dir_for(session_id)
    resolved = _resolve_or_raise(working_dir, path)
    return JSONResponse(
        {
            "path": resolved.relative_path,
            "kind": resolved.kind,
            "mime_type": resolved.mime_type,
            "size": resolved.size,
        }
    )


@router.get("/{session_id}/files")
async def get_file(
    session_id: str,
    path: str = Query(...),
    _: str = Depends(_verify_token),
) -> FileResponse:
    working_dir = await _working_dir_for(session_id)
    resolved = _resolve_or_raise(working_dir, path)
    # FileResponse handles streaming + ETag + Range. We override
    # media_type so e.g. .md is delivered as text/markdown rather
    # than the default application/octet-stream.
    return FileResponse(
        resolved.abs_path,
        media_type=resolved.mime_type,
        filename=resolved.abs_path.name,
    )
