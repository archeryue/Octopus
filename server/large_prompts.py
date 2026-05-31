"""Spill oversize prompts to disk and hand the backend a pointer.

Linux's `MAX_ARG_STRLEN` ceiling (~128 KB / 32 pages) means `execve`
fails with `E2BIG` if any single argv element is larger. Octopus
passes the user prompt to `claude` as a positional argv after `--`
(the VM0 shape — see `docs/post-mortems/2026-05-18-bg-pipeline-hardening.md` §2),
so any prompt over the ceiling is unspawnable.

In the wild this fires most often on bg-task-result injection
(test-suite stdout, `cargo build` output, big greps), but it can
also trip on a user pasting a large block directly into the chat.

Strategy: any prompt whose UTF-8 size exceeds
LARGE_PROMPT_THRESHOLD_BYTES is written to a per-session spill file,
and the backend receives a small **pointer prompt** that tells the
model to `Read` the file in full before responding. The model's Read
tool supports offset/limit and Grep, so prompts much larger than the
model's own context window are still consumable — the model decides
what to ingest.

Lifecycle: spill files live alongside attachments and are wiped by
`delete_session_large_prompts(session_id)` when the session is
deleted.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)

# 100 KB UTF-8 bytes. Sits comfortably under the 131,072-byte
# MAX_ARG_STRLEN cap with room for the other argv elements
# (--mcp-config JSON, --append-system-prompt text, --resume id, etc.)
# that share the same exec call.
LARGE_PROMPT_THRESHOLD_BYTES = 100 * 1024

# Markers that must survive the spill — if the original prompt
# starts with one of these, the pointer prompt carries it forward
# at the front so downstream consumers that key off the marker
# (e.g. the frontend's "auto" badge for bg-task-result) keep working.
_PRESERVED_MARKERS = ("[bg-task-result]", "[octopus-large-prompt]")


def _large_prompts_root() -> Path:
    """Spill root with `~` expanded. Mirrors attachments_root() — we
    don't mkdir here so importing the module is a no-op."""
    return Path(settings.large_prompts_dir).expanduser().resolve()


def _session_dir(session_id: str) -> Path:
    """Per-session spill directory. Created on demand.

    Guards against a malformed session_id scribbling outside the root
    — session ids are uuid4 hex prefixes in production, but defensive
    in case a future caller passes user input through.
    """
    if not session_id or "/" in session_id or "\\" in session_id:
        raise ValueError(f"invalid session_id for large-prompt spill: {session_id!r}")
    d = _large_prompts_root() / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _extract_preserved_marker(prompt: str) -> str | None:
    """Return the leading marker if the prompt starts with one, else None.

    Used so a spilled bg-task-result prompt still begins with
    `[bg-task-result]` in what the backend sees — the frontend keys
    off that exact prefix for the auto badge.
    """
    for marker in _PRESERVED_MARKERS:
        if prompt.startswith(marker):
            return marker
    return None


def spill_if_large(session_id: str, prompt: str) -> str:
    """Return the prompt unchanged if it fits in argv; otherwise spill
    it to disk and return a small pointer prompt for the backend.

    The pointer prompt:
      - preserves a recognized leading marker (so frontend/auto-badge
        logic that keys off `[bg-task-result]` keeps working)
      - states the absolute path to the spill file
      - explicitly instructs the model to Read it in full before
        responding (so it can't be missed)
    """
    byte_size = len(prompt.encode("utf-8"))
    if byte_size <= LARGE_PROMPT_THRESHOLD_BYTES:
        return prompt

    spill_dir = _session_dir(session_id)
    fname = f"{uuid.uuid4().hex}.txt"
    path = spill_dir / fname
    # Write atomically: write to a temp sibling, then rename. Avoids a
    # half-written file being observed by the model on a crash mid-write
    # (the model's Read would see truncated content). rename(2) within
    # the same directory is atomic on POSIX.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(prompt, encoding="utf-8")
    tmp.rename(path)

    abs_path = str(path)
    marker = _extract_preserved_marker(prompt)
    prefix = f"{marker} " if marker else ""
    pointer = (
        f"{prefix}[octopus-large-prompt] The actual user message is "
        f"{byte_size:,} bytes — too large to deliver inline. "
        f"It's saved at {abs_path}. Read that file in full to see the "
        f"message, then respond to it. Use Read with offset/limit and "
        f"Grep if it's larger than your context window."
    )
    logger.info(
        "Spilled %d-byte prompt for session %s to %s",
        byte_size, session_id, abs_path,
    )
    return pointer


def delete_session_large_prompts(session_id: str) -> None:
    """Wipe the session's spill dir. No-op if missing. Best-effort —
    a failed rmtree shouldn't block session deletion."""
    d = _large_prompts_root() / session_id
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
