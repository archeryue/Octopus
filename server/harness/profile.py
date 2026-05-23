"""The `RuntimeProfile` — one data record per harness kind.

This is the heart of the data-driven design (VM0's `Record<framework,…>`
shape in Python): there are no `ClaudeCodeHarness`/`CodexHarness`
subclasses. There is one `Harness` class, one `HarnessRun` engine, and
two `RuntimeProfile` *values* (`CLAUDE_CODE` in claude_code.py, `CODEX` in
codex.py) that supply the few genuinely per-framework pieces — argv
rendering, event parsing, one-shot, login, transcript codec — as data +
small collaborators.

The shared `assembly.py` pre-computes the neutral inputs (selected MCP
servers, composed system prompt) into a `TurnContext`; the profile's
`build_turn_argv` only renders that into a concrete CLI command.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .events import HarnessCredential, HarnessEvent
from .login import LoginDriver


@dataclass
class McpServerEntry:
    """Backend-neutral MCP server spec (the connector `mcp_entry` shape).

    Built once by `assembly.select_mcp_servers`; each profile renders the
    list into its own config form (`--mcp-config` JSON for Claude,
    `-c mcp_servers.*` TOML for Codex)."""

    key: str
    command: str
    args: list[str]
    env: dict[str, str]


@dataclass
class TurnContext:
    """Fully-assembled, neutral inputs for one turn — what `build_turn_argv`
    renders. The shared work (MCP selection, system-prompt composition,
    working-dir absolutization) already happened in `assembly.py`."""

    prompt: str
    working_dir: str                  # absolute
    resume_id: str | None
    system_prompt: str                # composed: persona + tools blurb + connectors
    model: str | None
    tool_allow: list[str] | None
    tool_deny: list[str] | None
    mcp_servers: list[McpServerEntry]  # selected built-ins + connectors
    credential: HarnessCredential | None


@dataclass
class OneShotContext:
    """Inputs for a lean, tool-free single model call (`run_oneshot`)."""

    prompt: str
    model: str | None = None
    credential: HarnessCredential | None = None
    working_dir: str | None = None


@dataclass
class ParseOutput:
    """Result of feeding one stdout JSON object to an `EventParser`."""

    events: list[HarnessEvent] = field(default_factory=list)
    end_of_stream: bool = False


class EventParser(ABC):
    """Per-turn stdout normalizer. A fresh instance is created for each run
    (`profile.new_event_parser()`), so it may hold the small per-turn state
    the protocols need — e.g. the captured session/thread id surfaced on
    `session_started` before `result` arrives."""

    @abstractmethod
    def parse(self, obj: dict[str, Any]) -> ParseOutput:
        """Map one parsed stdout JSON object to zero+ events, flagging
        end-of-stream when the turn's terminal event (result) lands."""


class TranscriptCodec(Protocol):
    """Read/write a harness's on-disk transcript format (export/import).

    Present only for harnesses that support handoff/pull (Claude's JSONL);
    `None` on a profile means export/import is unsupported (Codex)."""

    def parse_file(self, path: str) -> Any: ...
    def write_file(
        self,
        path: str,
        messages: list[Any],
        session_id: str | None,
        working_dir: str | None,
    ) -> None: ...


@dataclass(frozen=True)
class RuntimeProfile:
    """Everything that differs between harness kinds, as one record."""

    backend: str                 # "claude-code" | "codex" (matches the persisted field)
    binary: str                  # "claude" | "codex"
    tools_prompt: str            # in-app-tools blurb (per-framework wording)
    credential_style: str        # "env_secret" | "home_dir"
    # Internal Claude-CLI bug workaround flag (not a product capability):
    # the session_manager run loop respawns with "continue" after a
    # premature mid-turn exit only when this is set.
    premature_exit_recovery: bool
    # Codex reads stdin even with a positional prompt; closing it after
    # spawn gives EOF so it uses just the argv prompt. Claude: False.
    close_stdin_after_start: bool
    # Renderers / parsers (module functions in the profile's file):
    build_turn_argv: Callable[[TurnContext], tuple[list[str], dict[str, Any]]]
    new_event_parser: Callable[[], EventParser]
    build_oneshot_argv: Callable[[OneShotContext], tuple[list[str], dict[str, Any]]]
    parse_oneshot_stdout: Callable[[str], str]
    # Collaborators (optional features):
    login: LoginDriver | None = None
    transcript_codec: TranscriptCodec | None = None
