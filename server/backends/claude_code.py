"""Claude Code backend — talks directly to the `claude` CLI subprocess.

Wire format documented in `docs/cli-protocol-notes.md`. The CLI's
stream-json protocol carries both "data" events (assistant, user,
result, system) and a parallel control protocol on stdio for
permissions / interrupts / initialize.

This backend replaces the previous reliance on `claude-code-sdk`.
"""

from __future__ import annotations

import asyncio  # noqa: F401  — kept for future async helpers + back-compat type hints
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from .base import BackendCredential, BackendEvent
from .subprocess_jsonl import SubprocessJsonlBackend

logger = logging.getLogger(__name__)


# Repo root (the directory that contains the `server/` package). We
# need it twice in build_args — once to launch each MCP server via
# `-m server.mcp_servers.<name>` and once to set PYTHONPATH so the
# import works regardless of where claude was invoked from.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)


# System prompt addendum teaching the model about the in-process MCP
# tools we inject (viewer + bg). Appended via --append-system-prompt
# on every turn so the model has it even when resuming.
_OCTOPUS_SYSTEM_PROMPT = """\
== Octopus in-app tools ==

You have access to extra tools injected by the Octopus controller. \
They are first-class — call them whenever appropriate, not as a \
fallback.

[1] `mcp__viewer__show_file` — opens a file from the current working \
directory in an in-app viewer modal so the user can see it directly.

When to call it:
  - When the user types `/showme <path>` in chat, call show_file \
with that path. If the path doesn't exist exactly (typo, wrong \
extension, partial name), use Glob or LS to find the closest match \
first, then call show_file with the corrected path. Don't refuse — \
make a best-effort guess.
  - Proactively, when showing a file directly is clearer than \
quoting its contents in your reply — for example, when the user asks \
"what's in the README?", or right after you wrote/edited a file the \
user should see.

Supported types: Markdown (.md), code files (Python, JS/TS, Go, Rust, \
etc.), images (PNG/JPG/GIF/SVG/WebP), PDFs, plain text/log/CSV. \
Paths are sandboxed to the working directory.

After calling show_file, briefly tell the user what you opened — \
don't paste the file contents in your reply, since they're already \
seeing them in the viewer.

[2] `mcp__bg__run(command, description?)` — fire-and-forget a shell \
command that runs in the BACKGROUND across turns. Returns a task_id \
immediately. When the bg task finishes, Octopus injects a follow-up \
turn into this session with the captured output, and you respond \
then.

When to use bg_run vs Bash:
  - Use **bg_run** for anything that may take longer than ~30 seconds \
(long builds, full test suites, npm install, large fetches, model \
training, container builds, sleeps). The user shouldn't wait on those \
inside your reply.
  - Use the regular **Bash** tool for short commands (< 30s) where \
the user expects an immediate answer in your reply.

Pattern when you use bg_run:
  1. Call `bg_run("the command", description="what it is")` — pass a \
short human description so the user's UI chip is informative.
  2. In your reply, tell the user briefly what you started ("Running \
the test suite in the background — I'll report back when it \
finishes.") — do NOT wait.
  3. End your turn. A new turn will arrive automatically with the \
result, prefixed `[bg-task-result]`. Treat that prefix as a signal it \
was auto-injected, not user-typed.

Related: `mcp__bg__cancel(task_id)` to abort a running task, and \
`mcp__bg__list()` to see recent bg tasks for this session (useful if \
the chat history is too long to scroll for the task_id).

[3] `mcp__ask__user(questions: list[QuestionSpec])` — ask the user \
one or more clarification questions and BLOCK until they answer. Use \
this whenever you'd otherwise have called the built-in \
`AskUserQuestion` tool — that built-in is DISABLED in this \
environment; this is its drop-in replacement.

Each question is `{question, header?, multiSelect?, options: \
[{label, description}]}` — same schema as the legacy tool. Pass 1-4 \
questions per call. 2-4 options per question (the UI auto-adds an \
"Other" free-text option). Returns the user's answers as a single \
formatted text string you can read like a tool result.

When to use it: when a real choice depends on the user (auth method, \
library pick, naming, scope decisions) AND there isn't an obviously \
right answer. Don't use it for things you can decide yourself or \
verify from the codebase. Don't use it as a substitute for \
ExitPlanMode."""


# Vestigial type alias kept so external tests that constructed the
# backend with `permission_callback=` keep type-checking. The VM0
# shape never invokes the callback (no `can_use_tool` requests under
# `--dangerously-skip-permissions`), but accepting it keeps the
# constructor signature stable.
PermissionDecision = dict[str, Any]
PermissionCallback = Any


class ClaudeCodeBackend(SubprocessJsonlBackend):
    """Spawn `claude` and translate its JSONL stream into BackendEvents.

    Lifecycle: one CLI invocation per turn (mirrors the existing SDK
    behavior). `start(prompt, working_dir, resume_id)` spawns the CLI
    with the prompt as a positional argv; `stream()` yields events
    until `result`; `stop()` shuts the subprocess down.

    Command shape (since the VM0-shape migration to fix the CLI's
    premature-exit bug at large context — see
    `docs/cli-resume-synthetic-pair.md`):

      - Prompt is passed as a **positional argv** after `--`, not
        streamed as JSON on stdin. This avoids `--input-format=stream-json`,
        which empirically correlates with the CLI dropping tool_result
        events on stdout at large context scale.
      - **`--dangerously-skip-permissions`** replaces the
        `--permission-prompt-tool=stdio` control-protocol path. We
        relied on that protocol for two real features — per-tool
        permission decisions and AskUserQuestion interception — and
        both have replacements that don't need stdin: permissions are
        bypassed at the CLI level (the host is the only thing running
        these subprocesses anyway), and AUQ goes through the new
        `mcp__ask__user` tool (see server/mcp_servers/ask.py) that
        long-polls Octopus over HTTP for the answer.
      - The CLI's built-in `AskUserQuestion` is disabled via
        `--disallowedTools` so the model uniformly uses our MCP
        replacement.

    What we no longer have (intentionally dropped):
      - `initialize` control_request handshake — never needed once we
        stop sending stream-json input.
      - `interrupt` control_request — `stop()` already SIGTERMs the
        subprocess; the graceful control_request was a nicety, not
        load-bearing.
      - `_handle_can_use_tool` — no `can_use_tool` requests arrive
        when `--dangerously-skip-permissions` is set.

    Constructor params:
        model:      override the CLI's default model (rare; usually let
                    settings decide).
        session_id: the Octopus session id this backend is bound to.
                    Threaded into the MCP-server env (`OCTOPUS_SESSION_ID`)
                    so the bg/ask tools can call back to the right
                    session. None is fine in tests that don't exercise
                    those tools.

    permission_callback is retained as a constructor parameter for
    backward compatibility with tests, but with --dangerously-skip-permissions
    no can_use_tool requests are emitted, so the callback is never
    invoked in production. Kept so the signature doesn't change.
    """

    name = "claude-code"
    binary = "claude"

    # Default flags. We use bypassPermissions when no callback is set so
    # behavior is identical to the legacy SDK path's bypass mode.
    def __init__(
        self,
        permission_callback: PermissionCallback | None = None,
        model: str | None = None,
        session_id: str | None = None,
    ) -> None:
        super().__init__()
        # Accepted for back-compat (see PermissionCallback comment above);
        # never invoked under the VM0 shape.
        self._permission_cb = permission_callback
        self._model = model
        # Octopus session id this backend instance is bound to. Threaded
        # into the bg + ask MCP envs so those tools can call back to the
        # right session. None is fine in tests that don't exercise them.
        self._session_id = session_id
        # Capture the CLI's `system/init` session_id so we can attach it
        # to the `result` BackendEvent (the result event's own session_id
        # is the same value, but `init` arrives first and the field gives
        # us a fallback if `result` ever omits it).
        self._captured_session_id: str | None = None

    # ------------------------------------------------------------------ build / send prompt

    def build_args(
        self,
        prompt: str,
        working_dir: str,
        resume_id: str | None,
        credential: BackendCredential | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        # Inline MCP config registering our three in-process servers.
        # The CLI accepts JSON strings directly (per `claude --help`:
        # "Load MCP servers from JSON files or strings"). Tool names
        # presented to the model are `mcp__<server-key>__<tool-fn>`:
        #
        #   viewer: mcp__viewer__show_file
        #   bg:     mcp__bg__run / __cancel / __list
        #   ask:    mcp__ask__user   (replaces the built-in AskUserQuestion)
        #
        # All three MCP servers run as subprocesses of `claude` and
        # need to call back into the FastAPI process. They share the
        # same callback-env shape (API base, auth token, session id);
        # the viewer is the exception — it doesn't call back, it just
        # validates paths against the working_dir passed via env.
        from ..config import settings as _settings  # local import: avoid cycle at module load
        api_base = f"http://127.0.0.1:{_settings.port}"
        callback_env: dict[str, str] = {
            "OCTOPUS_API_BASE": api_base,
            "OCTOPUS_AUTH_TOKEN": _settings.auth_token,
            "PYTHONPATH": _REPO_ROOT,
        }
        if self._session_id:
            callback_env["OCTOPUS_SESSION_ID"] = self._session_id

        mcp_config = json.dumps(
            {
                "mcpServers": {
                    "viewer": {
                        "command": sys.executable,
                        "args": ["-m", "server.mcp_servers.viewer"],
                        "env": {
                            "OCTOPUS_WORKING_DIR": working_dir,
                            "PYTHONPATH": _REPO_ROOT,
                        },
                    },
                    "bg": {
                        "command": sys.executable,
                        "args": ["-m", "server.mcp_servers.bg"],
                        "env": callback_env,
                    },
                    "ask": {
                        "command": sys.executable,
                        "args": ["-m", "server.mcp_servers.ask"],
                        "env": callback_env,
                    },
                }
            }
        )

        # VM0-style command shape. Notes on each flag:
        #   --print                  one-shot mode (we drive turns ourselves).
        #   --output-format=stream-json  parse events from stdout.
        #   --verbose                required with --print + stream-json.
        #   --dangerously-skip-permissions   no `can_use_tool` round-trips;
        #                                    the user already trusts this
        #                                    subprocess (Octopus is the only
        #                                    thing spawning these claude
        #                                    invocations on their behalf).
        #   --disallowedTools AskUserQuestion   force the model to use
        #                                       mcp__ask__user (the MCP
        #                                       replacement) instead of the
        #                                       built-in.
        #   --mcp-config JSON        register the three in-process MCP
        #                            servers above.
        #   --append-system-prompt   teach the model about /showme,
        #                            bg_run, and ask_user.
        #   --resume <id>            continue the prior conversation when
        #                            present.
        #   --                       end of flag parsing; prompt follows.
        argv = [
            self.binary,
            "--print",
            "--output-format=stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--disallowedTools", "AskUserQuestion",
            "--mcp-config", mcp_config,
            "--append-system-prompt", _OCTOPUS_SYSTEM_PROMPT,
        ]
        if self._model:
            argv += ["--model", self._model]
        if resume_id:
            argv += ["--resume", resume_id]
        # `--` terminates option parsing; the prompt is the final arg.
        # Empty prompts are valid (used by the auto-respawn nudge path
        # if we ever add it) — pass an empty string rather than
        # omitting the arg, so the CLI sees an explicit empty turn
        # rather than "no prompt at all" (which it errors on).
        argv += ["--", prompt]

        env = os.environ.copy()
        if credential is not None:
            if credential.auth_type == "api_key":
                # Long-lived sk-ant- key: works as ANTHROPIC_API_KEY and
                # takes precedence over any cached `claude login` session.
                env["ANTHROPIC_API_KEY"] = credential.secret
            elif credential.auth_type == "oauth":
                # OAuth access token from a Pro/Max subscription. The
                # session_manager resolver refreshed it if needed before
                # handing it to us. CLAUDE_CODE_OAUTH_TOKEN is the env var
                # the CLI documents for headless subscription auth, and it
                # also takes precedence over the on-disk credentials file.
                env["CLAUDE_CODE_OAUTH_TOKEN"] = credential.secret
        return argv, {"cwd": working_dir, "env": env}

    async def send_initial_prompt(self, prompt: str) -> None:
        """No-op in VM0-shape: the prompt was passed as positional argv
        in build_args, so nothing to write on stdin. Kept as a method so
        SubprocessJsonlBackend's lifecycle calling convention is
        unchanged."""
        return

    # ------------------------------------------------------------------ event parsing

    async def on_stdout_line(self, line: str) -> None:
        obj = self.parse_json_line(line)
        if obj is None:
            return
        kind = obj.get("type")

        if kind == "system":
            # init/status/etc — capture session id on init, ignore otherwise.
            if obj.get("subtype") == "init":
                self._captured_session_id = obj.get("session_id")
            return

        if kind == "rate_limit_event":
            return

        if kind == "stream_event":
            # Partial deltas — only when --include-partial-messages.
            # We don't enable that flag today; ignore if it sneaks in.
            return

        if kind == "control_response":
            await self._handle_control_response(obj)
            return

        if kind == "control_request":
            await self._handle_control_request(obj)
            return

        if kind == "assistant":
            self._emit_assistant_blocks(obj.get("message", {}))
            return

        if kind == "user":
            # The CLI echoes tool_results back as user events.
            self._emit_user_blocks(obj.get("message", {}))
            return

        if kind == "result":
            self._emit_result(obj)
            return

        logger.debug("Unhandled CLI event type: %s", kind)

    def _emit_assistant_blocks(self, message: dict[str, Any]) -> None:
        for block in message.get("content", []):
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if not text.strip():
                    continue
                self._emit(BackendEvent(type="text", content=text, raw=block))
            elif btype == "thinking":
                # Persist but don't surface by default; session_manager
                # decides whether to show.
                self._emit(
                    BackendEvent(
                        type="thinking", content=block.get("thinking", ""), raw=block
                    )
                )
            elif btype == "tool_use":
                self._emit(
                    BackendEvent(
                        type="tool_use",
                        tool_name=block.get("name"),
                        tool_input=block.get("input"),
                        tool_use_id=block.get("id"),
                        raw=block,
                    )
                )

    def _emit_user_blocks(self, message: dict[str, Any]) -> None:
        content = message.get("content", [])
        if not isinstance(content, list):
            return
        for block in content:
            if block.get("type") == "tool_result":
                raw_content = block.get("content")
                if isinstance(raw_content, list):
                    raw_content = json.dumps(raw_content)
                self._emit(
                    BackendEvent(
                        type="tool_result",
                        content=raw_content,
                        tool_use_id=block.get("tool_use_id"),
                        is_error=bool(block.get("is_error")),
                        raw=block,
                    )
                )

    def _emit_result(self, obj: dict[str, Any]) -> None:
        sid = obj.get("session_id") or self._captured_session_id
        self._emit(
            BackendEvent(
                type="result",
                session_id=sid,
                cost=obj.get("total_cost_usd"),
                duration_ms=obj.get("duration_ms"),
                num_turns=obj.get("num_turns"),
                is_error=bool(obj.get("is_error")),
                raw=obj,
            )
        )
        # The CLI keeps stdin open until we close it, so its stdout reader
        # would otherwise stall here forever. The `result` event is the
        # end-of-turn signal — push the EOF sentinel so the stream iterator
        # returns and the caller can stop() us.
        self._close_stream()

    # ------------------------------------------------------------------ control protocol (vestigial)

    async def _handle_control_response(self, obj: dict[str, Any]) -> None:
        """No outgoing control_requests in the VM0 shape, so no
        responses ever match a pending future. Drop silently."""
        return

    async def _handle_control_request(self, obj: dict[str, Any]) -> None:
        """The CLI doesn't send `can_use_tool` under
        `--dangerously-skip-permissions`, but we may still see other
        control_request kinds (`mcp_message`, etc.) in unusual
        situations. Drop them silently — there's no longer a host
        callback to route them to."""
        return

    # ------------------------------------------------------------------ interrupt / answer_question

    async def interrupt(self) -> None:
        """Stop the CLI subprocess. Without the control protocol over
        stdin, there's no graceful `interrupt` request to send first —
        SubprocessJsonlBackend.stop() does SIGTERM → 2 s grace → SIGTERM
        again → 2 s → SIGKILL, which is sufficient. Any in-flight tool
        work in MCP-server children dies with their parent process
        group."""
        await self.stop()

    async def answer_question(self, question_id: str, answer_text: str) -> bool:
        """No-op in the VM0 shape: AskUserQuestion answers now flow
        through session_manager.answer_question → asyncio.Event →
        mcp__ask__user's HTTP long-poll. The backend doesn't need to
        do anything here. Kept on the interface for compatibility
        with callers that don't yet know about the new path."""
        return True
        return True
