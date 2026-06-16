"""Attachment upload/download for sessions.

Two endpoints, both auth-gated by the session-level bearer token:

  POST   /api/sessions/{session_id}/attachments   multipart upload
  GET    /api/sessions/{session_id}/attachments/{attachment_id}

Upload returns AttachmentMetadata so the frontend can render a chip
immediately and remember the id to include in the next send_message
WebSocket frame. The on-disk path stays server-side; clients only ever
see the metadata and fetch the file back via the GET endpoint.

Attachment storage layout + lifecycle live in `server/attachments.py`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse

from ..attachments import (
    AttachmentError,
    MAX_FILE_BYTES,
    get_path,
    get_path_with_fork_fallback,
    save_upload,
)
from ..auth import verify_token
from ..config import settings
from ..models import AttachmentMetadata
from ..session_manager import session_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["attachments"])


def _require_session(session_id: str) -> None:
    """404 if the session isn't in memory. We don't allow uploads to
    archived sessions — they're read-only history."""
    if session_manager.get_session(session_id) is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Session {session_id} not found"
        )


@router.post(
    "/{session_id}/attachments",
    response_model=AttachmentMetadata,
    status_code=status.HTTP_201_CREATED,
)
async def upload_attachment(
    session_id: str,
    file: UploadFile,
    _: str = Depends(verify_token),
) -> AttachmentMetadata:
    _require_session(session_id)

    # Read fully into memory: the cap is small (25 MB) and the storage
    # module needs the bytes for size + write. Streaming to disk first
    # would complicate the size-check error path.
    content = await file.read()
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"attachment exceeds {MAX_FILE_BYTES} bytes",
        )

    try:
        record = save_upload(
            session_id=session_id,
            filename=file.filename or "file",
            content=content,
            declared_mime=file.content_type,
        )
    except AttachmentError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    return AttachmentMetadata(
        id=record.id,
        filename=record.filename,
        size=record.size,
        mime_type=record.mime_type,
    )


def _verify_download_token(
    request: Request, token: str | None = Query(default=None)
) -> str:
    """Allow EITHER a bearer header OR `?token=…` query.

    The query path exists so `<img src="…/attachments/…?token=…">` works in
    the browser — image tags can't carry custom Authorization headers.
    Same auth value either way: there's no second, weaker token to leak.
    """
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        candidate = auth_header.split(" ", 1)[1].strip()
        if candidate == settings.auth_token:
            return candidate
    if token and token == settings.auth_token:
        return token
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")


@router.get("/{session_id}/attachments/{attachment_id}")
async def download_attachment(
    session_id: str,
    attachment_id: str,
    _: str = Depends(_verify_download_token),
) -> FileResponse:
    # Don't require the session to still exist in memory — once a message
    # references an attachment, the chat history should be able to render
    # the chip / thumbnail even if the session was just archived. Hard
    # delete wipes the files, so a missing file naturally 404s below.
    #
    # Fork fallback (session-rewind.md §5.1 step 5.2): a fork copies only
    # attachment metadata, so resolve from the fork's own dir first, then walk
    # its `forked_from_session_id` ancestors.
    path = get_path(session_id, attachment_id)
    if path is None:
        ancestors = await session_manager.fork_ancestor_ids(session_id)
        path = get_path_with_fork_fallback(ancestors, attachment_id)
    if path is None or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")

    # Filename in the response: strip the `<id>__` prefix we use on disk
    # so the browser's "Save As" suggests the user's original name.
    display_name = path.name.split("__", 1)[1] if "__" in path.name else path.name
    return FileResponse(path, filename=display_name)
