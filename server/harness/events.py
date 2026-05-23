"""Backend-neutral DTOs + errors for the harness layer.

`HarnessEvent` is the normalized event every harness run emits (the
vocabulary `session_manager` broadcasts on WS). `HarnessCredential` is a
resolved credential ready for a profile to apply at spawn — in one of two
shapes selected by the profile's `credential_style`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HarnessCredential:
    """Resolved credential, ready for a profile to apply to its subprocess.

    Two shapes, picked by the harness profile's `credential_style`:
      - ``env_secret`` (Claude): ``secret`` is a plaintext API key / OAuth
        token, applied as an env var (ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN).
      - ``home_dir`` (Codex): ``home_dir`` is a CODEX_HOME directory holding
        ``auth.json``; ``secret`` is unused.

    ``secret`` is plaintext at this point — decrypted upstream by the
    credential resolver. The profile is responsible for never logging it.
    """

    backend: str            # "claude-code" | "codex"
    auth_type: str          # "api_key" | "oauth"
    secret: str = ""        # plaintext key/token (env_secret style)
    home_dir: str | None = None   # CODEX_HOME dir (home_dir style)


@dataclass
class HarnessEvent:
    """Normalized event emitted by any harness run.

    The vocabulary mirrors what session_manager already broadcasts on WS,
    so the front-end doesn't change when we swap the underlying CLI.
    """

    type: str  # text | thinking | tool_use | tool_result | result | error | question_request | session_started
    content: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    is_error: bool = False
    cost: float | None = None
    session_id: str | None = None  # backend's resume id (carried on `result`)
    duration_ms: int | None = None
    num_turns: int | None = None
    raw: dict[str, Any] | None = field(default=None, repr=False)


class HarnessOneshotError(Exception):
    """A one-shot (`run_oneshot`) model call failed. `code` is a stable
    machine token (``not_found`` | ``timeout`` | ``failed`` | ``bad_output``
    | ``empty``) the caller maps to a domain-specific, user-facing message
    (e.g. schedule parsing → ScheduleParseError)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
