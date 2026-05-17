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
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from .base import BackendCredential, BackendEvent
from .subprocess_jsonl import SubprocessJsonlBackend

logger = logging.getLogger(__name__)


# Type for the can_use_tool permission callback. Returns either
# {"allow": True, "input"?: dict} or {"allow": False, "reason"?: str}.
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
    ) -> None:
        super().__init__()
        self._permission_cb = permission_callback
        self._model = model
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
        argv = [
            self.binary,
            "--print",
            "--input-format=stream-json",
            "--output-format=stream-json",
            "--verbose",
            "--permission-mode=default",
            "--permission-prompt-tool=stdio",
        ]
        if self._model:
            argv += ["--model", self._model]
        if resume_id:
            argv += ["--resume", resume_id]
        env = os.environ.copy()
        if credential is not None and credential.auth_type == "api_key":
            # ANTHROPIC_API_KEY takes precedence over any cached `claude login`
            # OAuth session. When None, the CLI falls back to its own auth
            # (keychain / ~/.claude). OAuth-typed credentials would need a
            # different application path — TBD when we implement OAuth.
            env["ANTHROPIC_API_KEY"] = credential.secret
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
        if self._permission_cb is None:
            await self._send_control_response_success(request_id, {"allow": True})
            return

        try:
            decision = await self._permission_cb(tool_name, tool_input)
        except Exception as e:
            await self._send_control_response_success(
                request_id, {"allow": False, "reason": f"callback error: {e}"}
            )
            return

        await self._send_control_response_success(request_id, decision)

    async def _send_control_response_success(
        self, request_id: str, response: dict[str, Any]
    ) -> None:
        payload = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": response,
            },
        }
        await self._write_stdin(json.dumps(payload) + "\n")

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
        # We deliver the answer via the deny `reason` channel (same trick
        # the SDK-era code used). Once we confirm what the CLI does with
        # the answer downstream (feature 7), this can switch to a
        # positive-result mechanism.
        await self._send_control_response_success(
            request_id, {"allow": False, "reason": answer_text}
        )
        return True
