"""The Claude Code runtime profile.

Everything Claude-specific lives here as data + collaborators referenced by
the `CLAUDE_CODE` `RuntimeProfile`: how to render a turn into a `claude
--print` command, how to normalize its stream-json output, the lean
one-shot call, the JSONL transcript codec (handoff/pull), and — wired in
Phase 4 with the credentials router — the OAuth login driver.

Ported faithfully from the former `backends/claude_code.py` (turn argv +
event normalization) and `schedule_ai.run_claude_oneshot` (one-shot). The
shared per-turn assembly (MCP selection, system-prompt composition,
working-dir absolutization) happens upstream in `assembly.py`, so
`build_turn_argv` only renders the already-neutral `TurnContext`.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from .events import HarnessCredential, HarnessEvent, HarnessOneshotError
from .harness import Harness
from .login import LoginMethod
from .profile import (
    EventParser,
    OneShotContext,
    ParseOutput,
    RuntimeProfile,
    TurnContext,
)
from .registry import register

logger = logging.getLogger(__name__)


# System-prompt addendum teaching the model about our in-process MCP tools
# (bg + ask). Appended via --append-system-prompt every turn so it survives
# --resume.
_OCTOPUS_SYSTEM_PROMPT = """\
== Octopus in-app tools ==

You have access to extra tools injected by the Octopus controller. \
They are first-class — call them whenever appropriate, not as a \
fallback.

[1] `mcp__bg__run(command, description?)` — fire-and-forget a shell \
command that runs in the BACKGROUND across turns. Returns a task_id \
immediately. When the bg task finishes, Octopus injects a follow-up \
turn into this session with the captured output, and you respond \
then.

When to use bg_run vs Bash — bright lines, not heuristics:

  **Use bg_run unconditionally for any of these, no matter how fast \
you think it will be**: test suites (`pytest`, `bun run test`, \
`bun run test:e2e`, `vitest`, `cargo test`, etc.), builds \
(`bun run build`, `cargo build`, `npm run build`, `tsc`, …), \
package installs (`pip install`, `bun install`, `npm install`, …), \
sleeps, large fetches/curls, anything that hits the network with \
unpredictable latency, anything you'd start with `&` in a shell. \
For these, *never* reach for Bash — even synchronously. The \
Claude Code harness will auto-background long Bash commands and \
those often get killed silently with empty captured output, which \
wastes minutes and confuses the user. bg_run is the only safe path.

  **Use Bash only for**: definitely-sub-10s things — `ls`, \
`git status`, `git log -n 5`, a single Edit's verification grep, \
one quick file read, a config dump. If you find yourself piping \
through `| tail -N` because you expect lots of output, that's \
already the wrong call — switch to bg_run and let the chip render \
the full output.

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

[2] `mcp__ask__user(questions: list[QuestionSpec])` — ask the user \
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
ExitPlanMode.

[3] `mcp__ask_agent__ask(name, request, files?)` — delegate work to \
another Octopus agent by display name. Use this when another agent \
is better placed for the job (different skills, different tool \
access, a fresh context). Returns a `delegation_id` IMMEDIATELY; the \
other agent runs in the background and Octopus auto-injects a \
follow-up turn here when they reply — prefixed \
`[agent-reply:<name> delegation=<id>]` for a normal reply, \
`[agent-question:<name> delegation=<id> question_id=<qid>]` if they \
need an answer, or `[agent-error:<name> delegation=<id> \
reason=<r>]` on failure / cancel.

When the user says things like "ask <name> to …", "have <name> \
review …", "delegate this to <name>", or "get <name>'s take on …" \
— that is a direct call to invoke `mcp__ask_agent__ask`. Do not \
paraphrase the request yourself, do not try to do the other agent's \
work; just call the tool with `name="<them>"` and a self-contained \
`request` string (the other agent does NOT see this session's \
transcript — write the request as if briefing a teammate who walked \
in cold). Optionally pass `files=["…"]` to point them at specific \
files in this session's working directory.

Pattern: call `mcp__ask_agent__ask`, briefly tell the user "asked \
<name>", then end your turn. When the follow-up `[agent-reply:…]` \
arrives, relay or build on what the other agent said.

When a `[agent-question:…]` turn arrives, decide: answer directly \
via `mcp__ask_agent__answer_agent_question(delegation_id, choice)` \
if you know the answer; ask the user via `mcp__ask__user` if you \
don't; cancel via `mcp__ask_agent__cancel_agent_task` as a last \
resort. The other agent never talks to anyone except you — \
questions and replies travel one hop, to the caller.

Related: `mcp__ask_agent__cancel_agent_task(delegation_id, reason?)` \
to stop an in-flight delegation, `mcp__ask_agent__list_agent_tasks()` \
to see recent delegations from this session."""


def _apply_env_credential(env: dict[str, str], credential: HarnessCredential | None) -> None:
    """Materialize an env_secret credential. api_key → ANTHROPIC_API_KEY
    (a long-lived sk-ant- key); oauth → CLAUDE_CODE_OAUTH_TOKEN (a refreshed
    Pro/Max access token). Both override any on-disk `claude login`."""
    if credential is None:
        return
    if credential.auth_type == "api_key":
        env["ANTHROPIC_API_KEY"] = credential.secret
    elif credential.auth_type == "oauth":
        env["CLAUDE_CODE_OAUTH_TOKEN"] = credential.secret


# ------------------------------------------------------------------ turn argv


def build_turn_argv(ctx: TurnContext) -> tuple[list[str], dict[str, Any]]:
    """Render a `claude --print` command for one turn (VM0 shape).

    Flags: `--print` one-shot mode; `--output-format=stream-json` + `--verbose`
    for parseable events; `--dangerously-skip-permissions` (the host is the
    only thing spawning this); `--disallowedTools AskUserQuestion` (force the
    `mcp__ask__user` replacement) plus any agent denies; `--mcp-config` JSON
    for the in-process servers; `--append-system-prompt`; optional
    `--allowedTools`/`--model`/`--resume`; `--` then the prompt."""
    mcp_config = json.dumps(
        {
            "mcpServers": {
                e.key: {"command": e.command, "args": e.args, "env": e.env}
                for e in ctx.mcp_servers
            }
        }
    )
    disallowed = ["AskUserQuestion", *(ctx.tool_deny or [])]

    argv = [
        "claude",
        "--print",
        "--output-format=stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--disallowedTools",
        ",".join(disallowed),
        "--mcp-config",
        mcp_config,
        "--append-system-prompt",
        ctx.system_prompt,
    ]
    if ctx.tool_allow:
        argv += ["--allowedTools", ",".join(ctx.tool_allow)]
    if ctx.model:
        argv += ["--model", ctx.model]
    if ctx.resume_id:
        argv += ["--resume", ctx.resume_id]
    argv += ["--", ctx.prompt]

    env = os.environ.copy()
    _apply_env_credential(env, ctx.credential)
    # Per-agent memory (docs/plans/memory.md §3): point Claude's auto-memory
    # dir at the agent's canonical store via the dedicated override. We do NOT
    # touch CLAUDE_CONFIG_DIR — that's the root of Claude's *session transcript*
    # store, so moving it would orphan every session's `--resume` data. The
    # override relocates only the memory dir; transcripts and auth stay in the
    # host config dir untouched.
    if ctx.memory_dir:
        env["CLAUDE_COWORK_MEMORY_PATH_OVERRIDE"] = ctx.memory_dir
    return argv, {"cwd": ctx.working_dir, "env": env}


# ------------------------------------------------------------------ event parsing


class ClaudeEventParser(EventParser):
    """Normalize `claude` stream-json into HarnessEvents. Holds the captured
    init session id so we can attach it to `result` (and surface it early on
    `session_started`, before the premature-exit recovery might need it)."""

    def __init__(self) -> None:
        self._captured_session_id: str | None = None

    def parse(self, obj: dict[str, Any]) -> ParseOutput:
        kind = obj.get("type")

        if kind == "system":
            if obj.get("subtype") == "init":
                sid = obj.get("session_id")
                self._captured_session_id = sid
                if sid:
                    return ParseOutput(
                        events=[HarnessEvent(type="session_started", session_id=sid)]
                    )
            return ParseOutput()

        # Partial deltas / rate-limit notices / vestigial control protocol —
        # nothing to surface under the VM0 shape.
        if kind in ("rate_limit_event", "stream_event", "control_response", "control_request"):
            return ParseOutput()

        if kind == "assistant":
            return ParseOutput(events=self._assistant_blocks(obj.get("message", {})))

        if kind == "user":
            return ParseOutput(events=self._user_blocks(obj.get("message", {})))

        if kind == "result":
            return ParseOutput(events=[self._result(obj)], end_of_stream=True)

        logger.debug("Unhandled CLI event type: %s", kind)
        return ParseOutput()

    def _assistant_blocks(self, message: dict[str, Any]) -> list[HarnessEvent]:
        out: list[HarnessEvent] = []
        for block in message.get("content", []):
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if not text.strip():
                    continue
                out.append(HarnessEvent(type="text", content=text, raw=block))
            elif btype == "thinking":
                out.append(
                    HarnessEvent(type="thinking", content=block.get("thinking", ""), raw=block)
                )
            elif btype == "tool_use":
                out.append(
                    HarnessEvent(
                        type="tool_use",
                        tool_name=block.get("name"),
                        tool_input=block.get("input"),
                        tool_use_id=block.get("id"),
                        raw=block,
                    )
                )
        return out

    def _user_blocks(self, message: dict[str, Any]) -> list[HarnessEvent]:
        content = message.get("content", [])
        if not isinstance(content, list):
            return []
        out: list[HarnessEvent] = []
        for block in content:
            if block.get("type") == "tool_result":
                raw_content = block.get("content")
                if isinstance(raw_content, list):
                    raw_content = json.dumps(raw_content)
                out.append(
                    HarnessEvent(
                        type="tool_result",
                        content=raw_content,
                        tool_use_id=block.get("tool_use_id"),
                        is_error=bool(block.get("is_error")),
                        raw=block,
                    )
                )
        return out

    def _result(self, obj: dict[str, Any]) -> HarnessEvent:
        sid = obj.get("session_id") or self._captured_session_id
        return HarnessEvent(
            type="result",
            session_id=sid,
            cost=obj.get("total_cost_usd"),
            duration_ms=obj.get("duration_ms"),
            num_turns=obj.get("num_turns"),
            is_error=bool(obj.get("is_error")),
            raw=obj,
        )


# ------------------------------------------------------------------ one-shot


def build_oneshot_argv(ctx: OneShotContext) -> tuple[list[str], dict[str, Any]]:
    """A lean, tool-free `claude --print --output-format=json` call."""
    argv = ["claude", "--print", "--output-format=json"]
    if ctx.model:
        argv += ["--model", ctx.model]
    argv += ["--", ctx.prompt]
    env = os.environ.copy()
    _apply_env_credential(env, ctx.credential)
    return argv, {"cwd": ctx.working_dir or os.getcwd(), "env": env}


def parse_oneshot_stdout(stdout: str) -> str:
    """Pull the model's text out of `--output-format=json` (the `result`
    field). Malformed JSON is a hard failure; an empty result is left for
    `run_oneshot` to flag as `empty`."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        raise HarnessOneshotError("bad_output", "unexpected one-shot response")
    text = data.get("result")
    return text if isinstance(text, str) else ""


# ------------------------------------------------------------------ transcript codec


class _JsonlTranscriptCodec:
    """Claude Code JSONL handoff/pull format (the only transcript codec)."""

    def parse_file(self, path: str) -> Any:
        from ..jsonl_parser import parse_jsonl_file

        return parse_jsonl_file(path)

    def write_file(
        self,
        path: str,
        messages: list[Any],
        session_id: str | None,
        working_dir: str | None,
    ) -> None:
        from ..jsonl_writer import write_jsonl_file

        write_jsonl_file(Path(path), messages, session_id or "", working_dir or "")


# ------------------------------------------------------------------ login driver


class _OAuthLoginDriver:
    """Claude's OAuth-redirect login, wrapping the OAuthLoginManager singleton.
    The user opens an authorize URL and pastes the returned code back."""

    method = LoginMethod.oauth_redirect

    async def start(self, label: str | None = None):
        from ..oauth_login import oauth_login_manager

        return await oauth_login_manager.start()

    async def submit_code(self, login_id: str, code: str):
        from ..oauth_login import oauth_login_manager

        return await oauth_login_manager.submit_code(login_id, code)

    def get(self, login_id: str):
        raise NotImplementedError("oauth_redirect login does not poll; use submit_code")

    async def cancel(self, login_id: str) -> None:
        from ..oauth_login import oauth_login_manager

        await oauth_login_manager.cancel(login_id)

    def cleanup_credential(self, credential_id: str) -> None:
        # Claude credentials are a secret blob in the DB — nothing on disk to
        # revoke; row deletion is sufficient.
        return None


# ------------------------------------------------------------------ profile


CLAUDE_CODE = RuntimeProfile(
    backend="claude-code",
    binary="claude",
    tools_prompt=_OCTOPUS_SYSTEM_PROMPT,
    credential_style="env_secret",
    premature_exit_recovery=True,
    close_stdin_after_start=False,
    build_turn_argv=build_turn_argv,
    new_event_parser=ClaudeEventParser,
    build_oneshot_argv=build_oneshot_argv,
    parse_oneshot_stdout=parse_oneshot_stdout,
    # Claude has native memory (auto-injects MEMORY.md); we point it at the
    # canonical per-agent dir via CLAUDE_COWORK_MEMORY_PATH_OVERRIDE in
    # build_turn_argv, so no system-prompt blurb is needed.
    injects_memory_prompt=False,
    login=_OAuthLoginDriver(),
    transcript_codec=_JsonlTranscriptCodec(),
)

register(Harness(CLAUDE_CODE))
