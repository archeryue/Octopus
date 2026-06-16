"""Fork contract DTOs for the harness layer (session-rewind.md §3).

A fork makes a parent session re-spawnable from a chosen branch point. The
*one* backend-specific concern is "synthesize a resumable transcript in this
backend's native format". Each profile picks a strategy:

  • NATIVE_TRANSCRIPT — synthesize a resumable transcript on disk at the
    backend's location using the caller-minted ``resume_id_hint`` as the
    artifact id; spawn resumes natively. (Claude.)
  • HISTORY_REPLAY — no on-disk work; return ``needs_replay=True`` so the
    first fork turn's user prompt is wrapped with the truncated history.
    (Codex.)

The harness owns both; callers above it (SessionManager, routers, frontend)
never branch on backend.
"""

from __future__ import annotations

from dataclasses import dataclass

# Strategy tokens — used only inside the harness layer for clarity.
NATIVE_TRANSCRIPT = "native_transcript"
HISTORY_REPLAY = "history_replay"


@dataclass
class ForkArtifact:
    """What ``Harness.prepare_fork`` returns.

    NATIVE backends return the backend-native resume handle in ``resume_id``
    (equal to the caller's hint) and ``needs_replay=False``. REPLAY backends
    return ``resume_id=None`` and ``needs_replay=True`` — their real resume id
    arrives later (Codex's ``thread.started``)."""

    resume_id: str | None
    needs_replay: bool


class BackendForkNotSupported(Exception):
    """Raised when a backend has no working fork strategy. Forward-compat —
    neither v1 backend raises it (both set ``profile.can_fork=True``)."""

    def __init__(self, backend: str) -> None:
        super().__init__(f"Backend {backend!r} does not support forking")
        self.backend = backend
