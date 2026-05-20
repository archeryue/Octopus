"""Abstract backend interface — all concrete backends emit BackendEvents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BackendCredential:
    """Resolved credential, ready for the backend to apply to its subprocess.

    `secret` is plaintext at this point — the encryption layer decrypted it
    before handing the credential to the backend. The backend is responsible
    for never logging it.
    """

    backend: str       # "claude-code" | "codex"
    auth_type: str     # "api_key" | "oauth"
    secret: str        # plaintext API key or OAuth token


@dataclass
class BackendEvent:
    """Normalized event emitted by any backend.

    The vocabulary mirrors what session_manager already broadcasts on WS,
    so the front-end doesn't need to change when we swap backends.
    """

    type: str  # text | thinking | tool_use | tool_result | result | error | question_request | session_started
    content: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    is_error: bool = False
    cost: float | None = None
    session_id: str | None = None  # backend's resume id (carried on `result` events)
    duration_ms: int | None = None
    num_turns: int | None = None
    raw: dict[str, Any] | None = field(default=None, repr=False)


class BackendBase(ABC):
    """Per-turn backend interface.

    Lifecycle: caller calls `start(...)`, then iterates `stream()` until it
    yields a `result` event (or raises), then calls `stop()`. Interrupt
    cancels the in-flight turn; answer_question delivers an AskUserQuestion
    answer that the backend may have requested via a `question_request` event.
    """

    name: str = "unknown"

    # Whether the session-manager run loop should apply the Claude-CLI
    # premature-exit-after-tool recovery (respawn with "continue" — see
    # docs/2026-05-18-bg-pipeline-hardening.md §2). It's a workaround for a
    # specific `claude` CLI bug; other backends (Codex) must not inherit it.
    wants_premature_exit_recovery: bool = False

    @abstractmethod
    async def start(
        self,
        prompt: str,
        working_dir: str,
        resume_id: str | None = None,
        credential: "BackendCredential | None" = None,
    ) -> None:
        """Spawn / connect the backend. Non-blocking — events come via stream().

        `credential`, if provided, gives the backend an authenticated identity
        to use for this run (typically applied as an env var on the subprocess).
        Falls back to whatever auth the CLI finds in its own config when None.
        """

    @abstractmethod
    def stream(self) -> AsyncIterator[BackendEvent]:
        """Async-iterate normalized events until the turn ends."""

    @abstractmethod
    async def stop(self) -> None:
        """Terminate the backend, releasing all resources. Idempotent."""

    async def interrupt(self) -> None:
        """Best-effort cancel of the current turn. Default impl: just stop."""
        await self.stop()

    async def answer_question(self, question_id: str, answer_text: str) -> bool:
        """Provide an answer for a pending AskUserQuestion-style prompt.

        Returns True if the backend accepted the answer. Default: not
        supported (returns False) — Codex doesn't have an equivalent.
        """
        return False
