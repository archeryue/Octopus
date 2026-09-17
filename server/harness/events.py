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
class SubagentUpdate:
    """One observation of a sub-agent the model spawned inside a turn.

    Both harnesses have this feature natively and describe it differently —
    Claude Code streams `system/task_*` events around a `Task` tool call,
    Codex streams `collab_tool_call` items carrying child thread ids. They
    normalize onto this one shape so the UI never learns which CLI it is
    talking to (native-subagents.md §3).

    A sub-agent is deliberately NOT a session: it is ephemeral, anonymous and
    scoped to one tool call, unlike a delegation, which is a real session
    owned by another agent (agent-collaboration.md).
    """

    task_id: str                     # the harness's own id for the run
    tool_use_id: str | None = None   # the parent tool call it belongs to
    status: str = "running"          # running | completed | failed
    name: str = ""                   # "Explore", "general-purpose", a thread id…
    description: str = ""            # what it is doing right now
    prompt: str = ""                 # the brief (first observation only)
    summary: str = ""                # final text (terminal observation)
    tokens: int | None = None
    tool_uses: int | None = None
    duration_ms: int | None = None

    def merged_with(self, older: "SubagentUpdate | None") -> "SubagentUpdate":
        """This observation, carrying forward anything it doesn't restate.

        Progress events are partial by design — `task_updated` is only a
        status patch, and `task_notification` doesn't repeat the name. The
        card needs the union, so the merge happens once here rather than in
        every consumer.
        """
        if older is None:
            return self
        return SubagentUpdate(
            task_id=self.task_id or older.task_id,
            tool_use_id=self.tool_use_id or older.tool_use_id,
            status=self.status or older.status,
            name=self.name or older.name,
            description=self.description or older.description,
            prompt=self.prompt or older.prompt,
            summary=self.summary or older.summary,
            tokens=self.tokens if self.tokens is not None else older.tokens,
            tool_uses=(
                self.tool_uses if self.tool_uses is not None else older.tool_uses
            ),
            duration_ms=(
                self.duration_ms
                if self.duration_ms is not None
                else older.duration_ms
            ),
        )


@dataclass
class HarnessEvent:
    """Normalized event emitted by any harness run.

    The vocabulary mirrors what session_manager already broadcasts on WS,
    so the front-end doesn't change when we swap the underlying CLI.
    """

    # text_delta is broadcast-only: a partial chunk of the text block still
    # being written. The completed `text` event always follows and is what
    # gets persisted (inline-steering.md §4 S1).
    # `subagent` is broadcast-only too: the durable record of a sub-agent is
    # the Task/collab tool call and its result, already persisted
    # (native-subagents.md §4).
    type: str  # text | text_delta | thinking | tool_use | tool_result | result | error | question_request | session_started | subagent
    content: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    is_error: bool = False
    cost: float | None = None
    session_id: str | None = None  # backend's resume id (carried on `result`)
    duration_ms: int | None = None
    num_turns: int | None = None
    # Set only on `subagent` events.
    subagent: SubagentUpdate | None = None
    raw: dict[str, Any] | None = field(default=None, repr=False)


class HarnessOneshotError(Exception):
    """A one-shot (`run_oneshot`) model call failed. `code` is a stable
    machine token (``not_found`` | ``timeout`` | ``failed`` | ``bad_output``
    | ``empty``) the caller maps to a domain-specific, user-facing message
    (e.g. schedule parsing → ScheduleParseError)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
