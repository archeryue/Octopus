"""On-disk storage for user-uploaded session attachments.

Files live at:
    <attachments_root>/<session_id>/<attachment_id>__<sanitized_filename>

`attachments_root` defaults to `~/.octopus/attachments` and is configurable
via `OCTOPUS_ATTACHMENTS_DIR`. Filenames keep the original extension (so
the agent's `Read` tool picks the right handler), prefixed with a short
unique id so two uploads of `screenshot.png` don't collide.

Lifecycle: attachments live as long as their owning session row. When a
session is hard-deleted (`DELETE /api/sessions/{id}`), the per-session
directory is removed too. There's no separate orphan GC — anything
uploaded but never sent gets wiped when its session is deleted.
"""

from __future__ import annotations

import mimetypes
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import settings

MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MB / file
MAX_ATTACHMENTS_PER_MESSAGE = 10

# Strip path separators, NUL, and other control chars from the user-supplied
# filename. We keep the rest (incl. unicode) so the agent sees something
# close to what the user uploaded.
_UNSAFE_CHAR_RE = re.compile(r"[\x00-\x1f\x7f/\\]+")
# `..` segments are removed entirely — collapsing them to `_` would still
# leave a recognisable shape that's easy to misread as path traversal.
_DOTDOT_RE = re.compile(r"\.{2,}")


class AttachmentError(ValueError):
    """Domain error raised by the attachments module.

    The HTTP layer maps these to 400/413 with the message visible to the
    client. Anything raised that isn't an AttachmentError is a bug and
    bubbles up as a 500.
    """


@dataclass(frozen=True)
class AttachmentRecord:
    id: str
    filename: str
    size: int
    mime_type: str
    path: Path  # absolute path on disk; what the agent's Read tool will open


def attachments_root() -> Path:
    """Return the configured attachments root, with `~` expanded.

    Created on demand by `session_dir`; we don't mkdir here so importing
    the module is a no-op.
    """
    return Path(settings.attachments_dir).expanduser().resolve()


def session_dir(session_id: str) -> Path:
    """Per-session subdirectory. Created if missing."""
    if not session_id or "/" in session_id or "\\" in session_id:
        # Defensive — session ids are uuid4 hex prefixes, never user input,
        # but a wrong id slipping in shouldn't be able to scribble outside
        # the root.
        raise AttachmentError(f"invalid session_id: {session_id!r}")
    d = attachments_root() / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sanitize_filename(name: str) -> str:
    """Reduce a user-supplied filename to something safe to put on disk.

    Strips path separators / control chars, collapses `..` runs, and caps
    length to 200 chars so the final path stays well under the FS limit
    even after the id prefix and parent dirs.
    """
    cleaned = _UNSAFE_CHAR_RE.sub("_", name or "").strip()
    cleaned = _DOTDOT_RE.sub("_", cleaned)
    cleaned = cleaned.strip("._") or "file"
    if len(cleaned) > 200:
        # Preserve the extension when truncating.
        stem, dot, ext = cleaned.rpartition(".")
        if dot and len(ext) <= 16:
            cleaned = stem[: 200 - len(ext) - 1] + "." + ext
        else:
            cleaned = cleaned[:200]
    return cleaned


def _detect_mime(filename: str, declared: str | None) -> str:
    """Trust the client's declared MIME if it looks plausible; otherwise
    fall back to the extension. application/octet-stream as a last resort.
    """
    if declared and "/" in declared and len(declared) < 200:
        return declared
    guess, _ = mimetypes.guess_type(filename)
    return guess or "application/octet-stream"


def save_upload(
    session_id: str,
    filename: str,
    content: bytes,
    declared_mime: str | None = None,
) -> AttachmentRecord:
    """Persist `content` under the session's attachment dir and return its
    metadata. Raises AttachmentError on size cap or empty payload.
    """
    if not content:
        raise AttachmentError("attachment is empty")
    if len(content) > MAX_FILE_BYTES:
        raise AttachmentError(
            f"attachment exceeds {MAX_FILE_BYTES} bytes "
            f"({len(content)} given)"
        )

    sanitized = _sanitize_filename(filename)
    attachment_id = uuid.uuid4().hex[:12]
    # `<id>__<name>` so listing the dir is human-readable AND `id` alone
    # is enough to recover the file (we glob for `<id>__*`).
    on_disk_name = f"{attachment_id}__{sanitized}"
    dest = session_dir(session_id) / on_disk_name
    dest.write_bytes(content)

    return AttachmentRecord(
        id=attachment_id,
        filename=sanitized,
        size=len(content),
        mime_type=_detect_mime(sanitized, declared_mime),
        path=dest.resolve(),
    )


def get_path(session_id: str, attachment_id: str) -> Path | None:
    """Resolve an attachment id back to its on-disk path. Returns None if
    the file isn't there (deleted, wrong id, etc.).

    Only the id is needed because the on-disk name is `<id>__<filename>`
    — we glob for the prefix so callers don't need to remember the
    sanitized filename.
    """
    if not attachment_id or "/" in attachment_id or "\\" in attachment_id:
        return None
    d = attachments_root() / session_id
    if not d.is_dir():
        return None
    matches = list(d.glob(f"{attachment_id}__*"))
    if not matches:
        return None
    return matches[0].resolve()


def delete_session_attachments(session_id: str) -> None:
    """Wipe the session's attachment dir. No-op if missing.

    Called from session_manager.delete_session. Best-effort: a failed
    rmtree shouldn't block the session delete itself, so we swallow.
    """
    d = attachments_root() / session_id
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
