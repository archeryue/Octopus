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

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse

from ..attachments import (
    MAX_FILE_BYTES,
    AttachmentError,
    get_path,
    get_path_with_fork_fallback,
    save_upload,
)
from ..auth import verify_token
from ..deps import ScopeUser, SessionMgr, ViewerUser
from ..models import AttachmentMetadata
from ..sessions import SessionManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["attachments"])


def _require_session(
    session_manager: SessionManager, session_id: str, user_id: str | None = None
) -> None:
    """404 if the session isn't in memory — or isn't this account's. We don't
    allow uploads to archived sessions: they're read-only history."""
    if session_manager.get_session(session_id, user_id) is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Session {session_id} not found"
        )


@router.post(
    "/{session_id}/attachments",
    response_model=AttachmentMetadata,
    status_code=status.HTTP_201_CREATED,
)
async def upload_attachment(
    session_manager: SessionMgr,
    session_id: str,
    file: UploadFile,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
) -> AttachmentMetadata:
    _require_session(session_manager, session_id, user_id)

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


@router.get("/{session_id}/attachments/{attachment_id}")
async def download_attachment(
    session_manager: SessionMgr,
    session_id: str,
    attachment_id: str,
    user_id: ViewerUser = None,
) -> FileResponse:
    """`ViewerUser` rather than `verify_token`, because an `<img src>` cannot
    carry an Authorization header — it admits the request from the header, the
    query or the cookie, and says whose sessions it may read."""
    if not await session_manager.session_belongs_to(session_id, user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")

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
