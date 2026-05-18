"""Claude Code backend — talks directly to the `claude` CLI subprocess.

Wire format documented in `docs/cli-protocol-notes.md`. The CLI's
stream-json protocol carries both "data" events (assistant, user,
result, system) and a parallel control protocol on stdio for
permissions / interrupts / initialize.

This backend replaces the previous reliance on `claude-code-sdk`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .base import BackendCredential, BackendEvent
from .subprocess_jsonl import SubprocessJsonlBackend

logger = logging.getLogger(__name__)


# Repo root (the directory that contains the `server/` package). We
# need it twice in build_args — once to launch the viewer MCP server
# via `-m server.mcp_servers.viewer` and once to set PYTHONPATH so
# that import works regardless of where claude is invoked from.
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
the chat history is too long to scroll for the task_id)."""


# Type for the can_use_tool permission callback. Returns the CLI's
# expected permission-result shape (Claude Code 2.x):
#   {"behavior": "allow", "updatedInput": dict}
#   {"behavior": "deny",  "message": str}
# `updatedInput` is required even when not modifying input — pass the
# original tool input through to honor the model's request as-is.
PermissionDecision = dict[str, Any]
PermissionCallback = Callable[[str, dict[str, Any]], Awaitable[PermissionDecision]]


class ClaudeCodeBackend(SubprocessJsonlBackend):
    """Spawn `claude` and translate its JSONL stream into BackendEvents.

    Lifecycle: one CLI invocation per turn (mirrors the existing SDK
    behavior). `start(prompt, working_dir, resume_id)` spawns, performs
    the `initialize` control handshake, then streams the prompt over
    stdin. `stream()` yields events until `result`. `stop()` shuts the
    subprocess down.

    Tool permissions:
        Provide a `permission_callback` when constructing. For every tool
        the CLI asks about, the callback is awaited; its return decides
        allow/deny. For AskUserQuestion specifically, the callback is
        expected to defer (await a Future) so the host can render UI and
        feed the answer back via `answer_question(question_id, text)` —
        the backend turns that into a synthetic Allow / Deny response.
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
        self._permission_cb = permission_callback
        self._model = model
        # Octopus session id this backend instance is bound to. Threaded
        # into the bg MCP env so the MCP tool knows which session to
        # attribute its bg_run calls to. None is fine in tests that
        # don't exercise bg.
        self._session_id = session_id
        # request_id → Future awaiting the response from the CLI (for
        # OUTGOING control requests we send, like initialize / interrupt).
        self._pending_outgoing: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # request_id → original incoming control_request (so we can answer
        # asynchronously when answer_question() is called).
        self._pending_incoming: dict[str, dict[str, Any]] = {}
        # Map AskUserQuestion question_id (we generate) → CLI request_id (to respond).
        self._question_to_request: dict[str, str] = {}
        self._req_counter = 0
        self._initialized = False
        self._captured_session_id: str | None = None

    # ------------------------------------------------------------------ build / send prompt

    def build_args(
        self,
        prompt: str,
        working_dir: str,
        resume_id: str | None,
        credential: BackendCredential | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        # Inline MCP config registering both our in-process servers
        # (viewer + bg). The CLI accepts JSON strings directly (per
        # `claude --help`: "Load MCP servers from JSON files or
        # strings"), so no temp file bookkeeping. Tool names presented
        # to the model are `mcp__<server-key>__<tool-fn>` —
        # `mcp__viewer__show_file`, `mcp__bg__run`, etc.
        #
        # viewer: needs OCTOPUS_WORKING_DIR (the session's working dir)
        #         so its sandbox helper resolves paths correctly.
        # bg:     needs OCTOPUS_API_BASE + OCTOPUS_AUTH_TOKEN +
        #         OCTOPUS_SESSION_ID so it can call back into the
        #         FastAPI process (it's a child of `claude`, not of
        #         FastAPI, so it has no direct handle to the manager).
        #         Loopback 127.0.0.1 because we always spawn the
        #         subprocess on the same host as the server.
        from ..config import settings as _settings  # local import: avoid cycle at module load
        api_base = f"http://127.0.0.1:{_settings.port}"
        bg_env: dict[str, str] = {
            "OCTOPUS_API_BASE": api_base,
            "OCTOPUS_AUTH_TOKEN": _settings.auth_token,
            "PYTHONPATH": _REPO_ROOT,
        }
        if self._session_id:
            bg_env["OCTOPUS_SESSION_ID"] = self._session_id

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
                        "env": bg_env,
                    },
                }
            }
        )

        argv = [
            self.binary,
            "--print",
            "--input-format=stream-json",
            "--output-format=stream-json",
            "--verbose",
            "--permission-mode=default",
            "--permission-prompt-tool=stdio",
            "--mcp-config",
            mcp_config,
            "--append-system-prompt",
            _OCTOPUS_SYSTEM_PROMPT,
        ]
        if self._model:
            argv += ["--model", self._model]
        if resume_id:
            argv += ["--resume", resume_id]
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
        # First do the initialize handshake (mirrors what the SDK does).
        await self._initialize()
        # Then stream the user turn.
        user_msg = {
            "type": "user",
            "message": {"role": "user", "content": prompt},
        }
        await self._write_stdin(json.dumps(user_msg) + "\n")

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

    # ------------------------------------------------------------------ control protocol

    async def _initialize(self) -> None:
        """Send the initialize handshake the SDK normally does."""
        await self._send_control_request(
            {"subtype": "initialize", "hooks": None}, timeout=30.0
        )
        self._initialized = True

    async def _send_control_request(
        self, request_body: dict[str, Any], timeout: float = 60.0
    ) -> dict[str, Any]:
        self._req_counter += 1
        request_id = f"oct_{self._req_counter}_{uuid.uuid4().hex[:6]}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_outgoing[request_id] = fut

        payload = {
            "type": "control_request",
            "request_id": request_id,
            "request": request_body,
        }
        await self._write_stdin(json.dumps(payload) + "\n")

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending_outgoing.pop(request_id, None)

    async def _handle_control_response(self, obj: dict[str, Any]) -> None:
        resp = obj.get("response", {})
        request_id = resp.get("request_id")
        if not request_id:
            return
        fut = self._pending_outgoing.get(request_id)
        if fut is None or fut.done():
            return
        if resp.get("subtype") == "error":
            fut.set_exception(RuntimeError(resp.get("error", "unknown control error")))
        else:
            fut.set_result(resp.get("response", {}))

    async def _handle_control_request(self, obj: dict[str, Any]) -> None:
        """CLI is asking us for a decision (permission, etc)."""
        request_id = obj.get("request_id")
        req = obj.get("request") or {}
        subtype = req.get("subtype")

        if not request_id or not subtype:
            return

        if subtype == "can_use_tool":
            await self._handle_can_use_tool(request_id, req)
            return

        # Unknown control request → respond with error so the CLI moves on.
        await self._send_control_response_error(
            request_id, f"unsupported control subtype: {subtype}"
        )

    async def _handle_can_use_tool(
        self, request_id: str, req: dict[str, Any]
    ) -> None:
        tool_name = req.get("tool_name", "")
        tool_input = req.get("input", {}) or {}

        # AskUserQuestion: emit a question_request event, hold the
        # control_request, wait for answer_question() to resolve it.
        if tool_name == "AskUserQuestion":
            question_id = uuid.uuid4().hex[:16]
            self._pending_incoming[request_id] = req
            self._question_to_request[question_id] = request_id
            self._emit(
                BackendEvent(
                    type="question_request",
                    tool_use_id=question_id,
                    tool_input=tool_input,
                    raw=req,
                )
            )
            return

        # Without a callback: blanket-allow (matches the legacy bypass).
        # The CLI's permission schema requires `updatedInput` even when we
        # aren't modifying it; pass the original tool input through.
        if self._permission_cb is None:
            await self._send_control_response_allow(request_id, tool_input)
            return

        try:
            decision = await self._permission_cb(tool_name, tool_input)
        except Exception as e:
            await self._send_control_response_deny(request_id, f"callback error: {e}")
            return

        # The callback returns the CLI-shaped decision directly. We don't
        # translate — that lets callers pass back e.g. `updatedInput` if
        # they want to rewrite the model's tool arguments.
        await self._send_control_response_success(request_id, decision)

    async def _send_control_response_success(
        self, request_id: str, response: dict[str, Any]
    ) -> None:
        """Wrap a permission-result dict in the control_response envelope.

        `response` must match the CLI's permission-result schema. Prefer
        the typed `_send_control_response_allow` / `_send_control_response_deny`
        helpers below; reach for this only when the caller already has a
        fully-formed decision dict (e.g. straight from a user callback).
        """
        payload = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": response,
            },
        }
        await self._write_stdin(json.dumps(payload) + "\n")

    async def _send_control_response_allow(
        self, request_id: str, updated_input: dict[str, Any] | None = None
    ) -> None:
        """Allow the tool to run, passing through (or rewriting) its input.

        The CLI requires `updatedInput` even when we don't intend to change
        anything — pass the original tool input through to honor the model's
        request as-is. Sending `{"allow": True}` (the legacy SDK shape) is
        rejected by the current CLI with a ZodError (see BUG_NEED_FIX #1).
        """
        await self._send_control_response_success(
            request_id,
            {"behavior": "allow", "updatedInput": updated_input or {}},
        )

    async def _send_control_response_deny(
        self, request_id: str, message: str
    ) -> None:
        """Deny the tool. `message` is what Claude sees as the rejection text."""
        await self._send_control_response_success(
            request_id,
            {"behavior": "deny", "message": message},
        )

    async def _send_control_response_with_content(
        self, request_id: str, content: str
    ) -> None:
        """Return content as the tool's effective result.

        We own the CLI subprocess and drive its control protocol
        directly, so the `behavior=deny, message=…` shape isn't a
        workaround — it's just the API word for "host-provided content
        replaces the tool's output". The deny `message` becomes what
        Claude sees as the tool's response. Used for AskUserQuestion
        answers: the user's selection is the content the tool would
        otherwise have gathered interactively.

        There's no MCP server to add or built-in tool to displace; the
        CLI hands us this channel for exactly this case.
        """
        await self._send_control_response_deny(request_id, content)

    async def _send_control_response_error(
        self, request_id: str, message: str
    ) -> None:
        payload = {
            "type": "control_response",
            "response": {
                "subtype": "error",
                "request_id": request_id,
                "error": message,
            },
        }
        await self._write_stdin(json.dumps(payload) + "\n")

    # ------------------------------------------------------------------ interrupt / answer_question

    async def interrupt(self) -> None:
        # Send the interrupt control request *before* tearing down — the
        # CLI may want to flush a final result event.
        if self._process and self._process.returncode is None:
            try:
                await asyncio.wait_for(
                    self._send_control_request({"subtype": "interrupt"}),
                    timeout=2.0,
                )
            except Exception:
                logger.debug("interrupt control request failed; proceeding to stop")
        await self.stop()

    async def answer_question(self, question_id: str, answer_text: str) -> bool:
        request_id = self._question_to_request.pop(question_id, None)
        if request_id is None:
            return False
        self._pending_incoming.pop(request_id, None)
        # Use the with_content path (not raw _send_control_response_deny)
        # to make the intent at the call site clear — we're returning a
        # tool result, not refusing the tool.
        await self._send_control_response_with_content(request_id, answer_text)
        return True
