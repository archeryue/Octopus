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
import re
from pathlib import Path
from typing import Any

from .events import (
    HarnessCredential,
    HarnessEvent,
    HarnessOneshotError,
    SubagentUpdate,
)
from .harness import Harness
from .login import LoginMethod
from .profile import (
    EventParser,
    OneShotContext,
    ParseOutput,
    RuntimeProfile,
    StdinMode,
    TurnContext,
    WebCapability,
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

[3] `mcp__ask_agent__ask(request, name=…, delegation_id=…, files=…)` \
— delegate work to another Octopus agent. Bimodal: pass `name` to \
start a fresh delegation under a target agent, or pass \
`delegation_id` (from a prior reply) to continue a previous \
delegation in the same child session — exactly one of the two must \
be set. Use a fresh delegation when another agent is better placed \
for the job (different skills, different tool access, a fresh \
context). Returns a `delegation_id` IMMEDIATELY; the other agent \
runs in the background and Octopus auto-injects a follow-up turn \
here when they reply — prefixed `[agent-reply:<name> delegation=<id>]` \
for a normal reply, `[agent-question:<name> delegation=<id> \
question_id=<qid>]` if they need an answer, or `[agent-error:<name> \
delegation=<id> reason=<r>]` on failure / cancel.

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
via `mcp__ask_agent__answer(delegation_id, choice)` if you know \
the answer; ask the user via `mcp__ask__user` if you don't; cancel \
via `mcp__ask_agent__cancel` as a last resort. The other agent \
never talks to anyone except you — questions and replies travel \
one hop, to the caller.

**Continuing the same line of work with the same agent** (review \
rounds, iterations on the same artifact, "now apply the same review \
to file Y") — don't omit the prior delegation_id and start over from \
scratch. Instead call `mcp__ask_agent__ask` again but pass the \
PRIOR `delegation_id` (and omit `name`): it reuses the same child \
session, so the other agent still has the previous turn in their \
transcript and can build on it without re-reading anything. Use the \
`name`-only form for fresh / unrelated / parallel work — multiple \
in-flight delegations to one target need separate sessions to run \
concurrently. Exactly one of (`name`, `delegation_id`) must be set.

Related: `mcp__ask_agent__cancel(delegation_id, reason?)` to stop \
an in-flight delegation, `mcp__ask_agent__list()` to see recent \
delegations from this session.

[4] `mcp__schedule__create(prompt, cron=…|interval_seconds=…|run_at=…, name=…, timezone=…, in_session=True)` — give yourself a schedule. Octopus keeps it durably (it survives restarts) and sends you `prompt` as a new turn each time it fires, prefixed `[scheduled:<name> — <recurrence>]`.

When the user says "every morning…", "every Monday…", "check this hourly", "remind me in two hours", "from now on, at 9am…" — that is this tool, not something to promise and forget. You cannot stay awake between turns: a `sleep`, a bg task that waits, or "I'll check back later" are all ways of not doing it. Set the schedule.

Pass exactly one recurrence: `cron="0 9 * * 1-5"` (5-field crontab, the right choice for clock times), `interval_seconds=1800` (every N seconds, min 60), or `run_at="2026-09-22T15:00"` (once, then it deletes itself). Timezone defaults to the host's; pass `timezone` when the user names another. Write `prompt` self-contained — when it fires, this conversation's context is long gone and nobody is waiting to answer questions.

Related: `mcp__schedule__list()` (ids, next fire times — call it before changing anything), `mcp__schedule__update(schedule_id, enabled=False | cron=… | prompt=…)` (pausing is usually what "stop doing X for now" means), `mcp__schedule__delete(schedule_id)`."""


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


# Where the prompt goes, and therefore what stdin is for. Referenced by both
def _render_subagents(defs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Octopus's sub-agent definitions as Claude Code's `--agents` JSON.

    Shape: `{"<name>": {"description": …, "prompt": …, "tools": [...],
    "model": …}}`. Entries without a name are dropped rather than sent as
    `""`, and empty optional fields are omitted so the CLI applies its own
    defaults instead of an explicit blank.
    """
    out: dict[str, dict[str, Any]] = {}
    for entry in defs:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        spec: dict[str, Any] = {
            "description": str(entry.get("description") or "").strip(),
            "prompt": str(entry.get("prompt") or "").strip(),
        }
        tools = entry.get("tools")
        if isinstance(tools, list) and tools:
            spec["tools"] = [str(t) for t in tools]
        model = entry.get("model")
        if model:
            spec["model"] = str(model)
        out[name] = spec
    return out


# The `system` subtypes Claude Code uses to narrate a sub-agent (`Task`) run.
# `task_updated` is the anonymous one — see `_task_event`.
_TASK_SUBTYPES = frozenset(
    {"task_started", "task_progress", "task_updated", "task_notification"}
)

# How many sub-agent runs the parser remembers names for. One entry is ~100
# bytes and a turn spawns a handful; this exists only so a process held across
# hundreds of turns can't grow one.
_MAX_TRACKED_TASKS = 64

# `build_turn_argv` and the profile below so the renderer and the run engine
# can never disagree (inline-steering.md §6).
_STDIN_MODE = StdinMode.STREAM_JSON


# ------------------------------------------------------------------ turn argv


# Tools a deep-research web leaf must NOT have: anything that writes/executes
# on the host, or spawns nested subagents (which would recurse the very fan-out
# we're orchestrating). native-deep-research.md §4.
_CLAUDE_WEB_LEAF_DENY = (
    "Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "Task",
)


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
    if ctx.web_research:
        # A research leaf may search/read the web but must not touch the box or
        # fan out its own subagents (native-deep-research.md §4). Deny the
        # destructive/exec + subagent tools; WebSearch/WebFetch/Read/etc. stay
        # available. (--allowedTools semantics vary with skip-permissions, so we
        # use a denylist, which is unambiguous.)
        disallowed += [
            t for t in _CLAUDE_WEB_LEAF_DENY if t not in disallowed
        ]

    argv = [
        "claude",
        "--print",
        "--output-format=stream-json",
        "--verbose",
        # Token deltas as they arrive. Without this the UI can't paint a word
        # until the whole block is done — measured 3.95s to first visible text
        # versus 1.68s with it, on the same turn (inline-steering.md §3).
        "--include-partial-messages",
        "--dangerously-skip-permissions",
        "--disallowedTools",
        ",".join(disallowed),
        "--mcp-config",
        mcp_config,
        "--append-system-prompt",
        ctx.system_prompt,
    ]
    if ctx.subagents:
        # Session-scoped sub-agent definitions (native-subagents.md §6). The
        # CLI's own built-ins (Explore, Plan, general-purpose…) stay available
        # alongside these; `--agents` adds, it doesn't replace.
        argv += ["--agents", json.dumps(_render_subagents(ctx.subagents))]
    if ctx.tool_allow:
        argv += ["--allowedTools", ",".join(ctx.tool_allow)]
    if ctx.model:
        argv += ["--model", ctx.model]
    if ctx.resume_id:
        argv += ["--resume", ctx.resume_id]
    if _STDIN_MODE is StdinMode.STREAM_JSON:
        # The prompt — and, later, any mid-turn follow-up — are written as
        # JSON lines on stdin instead of being baked into argv
        # (inline-steering.md §6). `HarnessRun.start` writes the first frame
        # the moment the process is up, so the CLI never waits on an idle pipe.
        argv += ["--input-format", "stream-json"]
    else:
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
        # task_id -> (tool_use_id, subagent name). `system/task_updated`
        # arrives carrying only a task_id — it is the one sub-agent event
        # that can't identify which tool call it belongs to
        # (native-subagents.md §3).
        self._tasks: dict[str, tuple[str | None, str]] = {}

    def parse(self, obj: dict[str, Any]) -> ParseOutput:
        kind = obj.get("type")

        if kind == "system":
            subtype = obj.get("subtype")
            if subtype == "init":
                sid = obj.get("session_id")
                self._captured_session_id = sid
                if sid:
                    return ParseOutput(
                        events=[HarnessEvent(type="session_started", session_id=sid)]
                    )
            if subtype == "api_retry":
                return self._api_retry(obj)
            if subtype in _TASK_SUBTYPES:
                return self._task_event(subtype, obj)
            return ParseOutput()

        if kind == "stream_event":
            return self._stream_delta(obj)

        # Rate-limit notices / vestigial control protocol — nothing to surface.
        if kind in ("rate_limit_event", "control_response", "control_request"):
            return ParseOutput()

        if kind == "assistant":
            return ParseOutput(events=self._assistant_blocks(obj.get("message", {})))

        if kind == "user":
            return ParseOutput(events=self._user_blocks(obj.get("message", {})))

        if kind == "result":
            return ParseOutput(events=[self._result(obj)], end_of_stream=True)

        logger.debug("Unhandled CLI event type: %s", kind)
        return ParseOutput()

    def _stream_delta(self, obj: dict[str, Any]) -> ParseOutput:
        """`--include-partial-messages` token deltas (inline-steering.md §4 S1).

        Only `content_block_delta` carries new characters; every other envelope
        frame (`message_start`/`_stop`, `content_block_start`/`_stop`,
        `message_delta`) describes structure the completed `assistant` event
        already gives us.

        These are **broadcast-only**: the authoritative text is the finished
        block, which still arrives and is what gets persisted. A dropped delta
        therefore costs a flicker, never a message — which is why the UI
        *replaces* its buffer with the final block rather than appending to it
        (§12).

        `thinking_delta` is deliberately not emitted: thinking is persisted but
        never broadcast (`_event_to_ws_message`), so streaming it would put text
        on screen that the completed turn then hides.
        """
        event = obj.get("event") or {}
        if event.get("type") != "content_block_delta":
            return ParseOutput()
        delta = event.get("delta") or {}
        if delta.get("type") != "text_delta":
            return ParseOutput()
        text = delta.get("text")
        if not text:
            return ParseOutput()
        return ParseOutput(events=[HarnessEvent(type="text_delta", content=text)])

    def _task_event(self, subtype: str, obj: dict[str, Any]) -> ParseOutput:
        """`system/task_*` → one normalized `subagent` event.

        The CLI reports a sub-agent in four shapes (started / progress /
        updated / notification), each partial. They are merged downstream; the
        job here is only to name the run, say what it is doing, and carry
        whatever counters this particular shape happens to include.
        """
        task_id = str(obj.get("task_id") or "")
        if not task_id:
            return ParseOutput()

        known_tool_use_id, known_name = self._tasks.get(task_id, (None, ""))
        tool_use_id = obj.get("tool_use_id") or known_tool_use_id
        name = obj.get("subagent_type") or known_name
        self._tasks[task_id] = (tool_use_id, name)

        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
        patch = obj.get("patch") if isinstance(obj.get("patch"), dict) else {}
        status = str(obj.get("status") or patch.get("status") or "running")
        # The CLI's terminal words are "completed" / "failed" / "cancelled";
        # anything else is still in flight.
        if status not in ("completed", "failed", "cancelled"):
            status = "running"

        update = SubagentUpdate(
            task_id=task_id,
            tool_use_id=tool_use_id,
            status=status,
            name=name,
            description=str(obj.get("description") or ""),
            prompt=str(obj.get("prompt") or "") if subtype == "task_started" else "",
            summary=str(obj.get("summary") or ""),
            tokens=usage.get("total_tokens"),
            tool_uses=usage.get("tool_uses"),
            duration_ms=usage.get("duration_ms"),
        )
        # The map is NOT dropped on a terminal status: `task_updated` says
        # "completed" and `task_notification` — which carries the summary —
        # arrives after it, anonymous but for the task id. Bounded instead,
        # because a held process parses many turns.
        while len(self._tasks) > _MAX_TRACKED_TASKS:
            self._tasks.pop(next(iter(self._tasks)))
        return ParseOutput(
            events=[HarnessEvent(type="subagent", subagent=update, raw=obj)]
        )

    def _api_retry(self, obj: dict[str, Any]) -> ParseOutput:
        """`system/api_retry` — the CLI retrying a failed API call itself.

        Retryable statuses (429/5xx/overloaded) are the CLI's business: stay
        quiet and let it work, and let the terminal failure fall through to
        the transient-retry path (harness-transient-retry.md).

        A **401 is different**. The credential is rejected, and no amount of
        retrying fixes that — but the CLI backs off exponentially across 10
        attempts, so the turn would sit there for roughly ten minutes before
        failing, long enough for the idle watchdog to mis-report it as a
        timeout. Surface it as an auth error immediately and end the stream;
        the session manager's reactive auth-expiry path then flags the
        credential `needs_reconnect` and prompts a re-authorize
        (harness-credential-reauth.md §4). The text deliberately contains
        "API error: 401" so `is_auth_error` matches it.
        """
        status = obj.get("error_status")
        reason = str(obj.get("error") or "").strip()
        if status != 401 and reason != "authentication_failed":
            return ParseOutput()
        detail = f" ({reason})" if reason else ""
        return ParseOutput(
            events=[
                HarnessEvent(
                    type="error",
                    content=(
                        f"Claude API error: 401 authentication failed{detail} — "
                        f"the credential was rejected."
                    ),
                    is_error=True,
                    raw=obj,
                )
            ],
            end_of_stream=True,
        )

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


# ------------------------------------------------------------------ fork (HISTORY_REPLAY)
#
# Claude forks use HISTORY_REPLAY, NOT NATIVE_TRANSCRIPT (session-rewind.md
# §5.3 + Phase-5 finding). We originally synthesized a resumable JSONL on disk
# and spawned `claude --resume <id>`, but the real CLI resolves `--resume`
# against a session-discovery path that does NOT reliably see an externally
# written transcript: through the production spawn path it fails ~all the time
# with "No conversation found", silently starting a fresh (empty) session.
# `claude` resumes its OWN sessions reliably, so HISTORY_REPLAY sidesteps the
# problem: the fork's FIRST turn carries the truncated parent transcript wrapped
# into its user prompt (a normal fresh `claude --print` turn — done in
# SessionManager.send_message), and turn 2+ resumes claude's own captured
# session id natively. Same contract + trade-offs as Codex (a heavier first
# turn, no parent-prefix cache reuse). NATIVE_TRANSCRIPT can return as a cache
# optimization if/when the CLI gains reliable external-transcript resume.


async def _fork_prepare_replay(
    messages: list[Any],
    working_dir: str,
    resume_id_hint: str | None,
    fork_id: str,
) -> "Any":
    """No on-disk work. The first fork turn's user prompt is wrapped with the
    truncated history (in SessionManager.send_message); `session_started`
    captures claude's real session id on turn 1; turn 2+ uses native resume of
    that own-session id. The `resume_id_hint` is ignored. Used by /rewind."""
    from .fork import ForkArtifact

    return ForkArtifact(resume_id=None, needs_replay=True)


def _claude_project_dir(working_dir: str) -> Path:
    """Claude stores each session transcript under
    ``~/.claude/projects/<cwd-slug>/<session_id>.jsonl``, where the slug is the
    absolute cwd with EVERY non-alphanumeric character replaced by ``-`` —
    verified against the real CLI: ``/`` ``\\`` ``.`` ``_`` etc. all map to ``-``
    (``/home/u/.octopus/fork/my_proj`` -> ``-home-u--octopus-fork-my-proj``),
    with no run-collapsing. Honors CLAUDE_CONFIG_DIR if set, else ~/.claude."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", working_dir)
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(cfg) if cfg else Path.home() / ".claude"
    return base / "projects" / slug


async def _fork_copy(
    *,
    parent_working_dir: str,
    parent_resume_id: str | None,
    parent_credential: Any = None,  # unused: Claude transcripts live under ~/.claude
    dest_working_dir: str,
    new_resume_id: str,
) -> "Any":
    """Full-copy fork (session-fork.md): copy the parent's REAL transcript
    into the fork's project slug under `new_resume_id`, rewriting each line's
    `cwd` -> dest and `sessionId` -> new id. The fork then resumes natively with
    the whole conversation as real context — no history replay. Verified: the
    CLI resumes such a copied (own-format) transcript reliably; the earlier
    'No conversation found' was specific to SYNTHESIZED transcripts. Falls back
    to replay when the parent has no transcript yet (never ran a turn)."""
    from .fork import ForkArtifact

    if not parent_resume_id:
        return ForkArtifact(resume_id=None, needs_replay=True)
    src = _claude_project_dir(parent_working_dir) / f"{parent_resume_id}.jsonl"
    if not src.is_file():
        return ForkArtifact(resume_id=None, needs_replay=True)

    dest_dir = _claude_project_dir(dest_working_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{new_resume_id}.jsonl"
    # Write to a temp file then atomically rename, so a crash mid-copy can't
    # leave a half-written transcript that later looks resumable (Vera review).
    tmp = dest.with_suffix(".jsonl.tmp")
    try:
        with src.open() as fin, tmp.open("w") as fout:
            for line in fin:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    fout.write(line + "\n")
                    continue
                if d.get("sessionId") == parent_resume_id:
                    d["sessionId"] = new_resume_id
                if d.get("cwd"):
                    d["cwd"] = dest_working_dir
                fout.write(json.dumps(d) + "\n")
        os.replace(tmp, dest)
    except Exception:
        tmp.unlink(missing_ok=True)  # don't leave partial temp junk behind
        raise
    return ForkArtifact(resume_id=new_resume_id, needs_replay=False)


async def _fork_cleanup(
    working_dir: str,
    resume_id_hint: str | None,
    fork_id: str,
    *,
    credential: Any = None,
) -> None:
    """Remove a copied transcript left by an incomplete full-copy saga
    (session-fork.md). The transcript lives under ~/.claude/projects, NOT
    inside the working dir, so removing the fork's copied dir doesn't reach it —
    this does. No-op for /rewind (replay leaves no transcript: the file named by
    the unused hint doesn't exist)."""
    if not resume_id_hint:
        return
    f = _claude_project_dir(working_dir) / f"{resume_id_hint}.jsonl"
    try:
        f.unlink()
    except FileNotFoundError:
        pass  # already gone — idempotent
    except OSError:
        # RE-RAISE: callers (compensation / startup sweep) treat a returning
        # cleanup as success and then delete the DB row, which would strand this
        # transcript forever. Surfacing keeps the row for a later retry (Vera).
        logger.exception("fork %s: failed to remove copied transcript %s", fork_id, f)
        raise


# ------------------------------------------------------------------ login driver


class _OAuthLoginDriver:
    """Claude's OAuth-redirect login, wrapping the OAuthLoginManager singleton.
    The user opens an authorize URL and pastes the returned code back."""

    method = LoginMethod.oauth_redirect

    async def start(
        self, label: str | None = None, *, reauth_credential_id: str | None = None
    ):
        # Claude re-auth targets the existing credential on the `complete`
        # route (it carries `credential_id`), so the redirect start itself is
        # identical for fresh and re-auth logins — nothing to thread here.
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


# Phrases the Claude CLI / Anthropic API emit when the credential is bad —
# a revoked/rotated key or an expired OAuth token (harness-credential-reauth.md
# §3). Specific enough to not fire on an unrelated 401 a *tool* surfaces.
# What the CLI prints on stderr when `--resume <id>` names a conversation it
# doesn't have: "No conversation found with session ID: <uuid>". It exits 1
# with a `result` of subtype `error_during_execution`, zero turns and zero
# cost — a silent, permanent failure for that session until the id is cleared.
_CLAUDE_STALE_SESSION_PATTERNS = (
    "no conversation found with session id",
    "no conversation found with session_id",
)

_CLAUDE_AUTH_ERROR_PATTERNS = (
    "invalid authentication credentials",
    "authentication_error",
    "invalid x-api-key",
    "invalid api key",
    "oauth token has expired",
    "oauth token is invalid",
    "api error: 401",
    "401 unauthorized",
    "please run /login",
    "invalid_grant",
)

# Transient provider-reliability failures worth an automatic retry
# (harness-transient-retry.md §3). Server-side 5xx / overload / dropped
# connection — plus Anthropic's SERVER-side throttle, which the CLI annotates
# "(not your usage limit)". We still must NOT retry the user's OWN quota/usage
# limit, so we match the throttle by its specific phrasing ("temporarily
# limiting requests", "not your usage limit") rather than a bare "rate limit"
# (which also appears in the user's-limit message). No auth phrases here.
_CLAUDE_TRANSIENT_ERROR_PATTERNS = (
    "overloaded",
    "api error: 500",
    "api error: 502",
    "api error: 503",
    "api error: 504",
    "api error: 529",
    "internal server error",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "connection error",
    "connection reset",
    "connection refused",
    "request timed out",
    "timed out",
    "the server had an error",
    "stream disconnected",
    # Server-side throttle (NOT the user's usage limit) — retryable.
    "temporarily limiting requests",
    "not your usage limit",
)


CLAUDE_CODE = RuntimeProfile(
    backend="claude-code",
    binary="claude",
    tools_prompt=_OCTOPUS_SYSTEM_PROMPT,
    credential_style="env_secret",
    premature_exit_recovery=True,
    auth_error_patterns=_CLAUDE_AUTH_ERROR_PATTERNS,
    stale_session_patterns=_CLAUDE_STALE_SESSION_PATTERNS,
    transient_error_patterns=_CLAUDE_TRANSIENT_ERROR_PATTERNS,
    web=WebCapability(tool_names=("WebSearch", "WebFetch"), combined=False),
    # The prompt is a JSON frame on stdin, which therefore stays open for the
    # life of the process — that open pipe is what lets one process serve
    # several turns, and (S3) take a steer mid-turn.
    #
    # Stdin used to be closed right after spawn, because an open-but-idle pipe
    # made the CLI wait ~3s ("no stdin data received in 3s") on every turn AND
    # made `--resume` of a freshly-synthesized fork transcript fail with "No
    # conversation found" (a discovery race the wait widened). STREAM_JSON
    # removes the wait by construction: the frame is written the instant the
    # process is up, so the CLI never waits for input that isn't coming
    # (measured 2.39s vs a 2.04s bare spawn). Neither failure reproduced —
    # inline-steering.md §10, with the fork suites as the standing gate.
    stdin_mode=_STDIN_MODE,
    build_turn_argv=build_turn_argv,
    new_event_parser=ClaudeEventParser,
    build_oneshot_argv=build_oneshot_argv,
    parse_oneshot_stdout=parse_oneshot_stdout,
    # Claude has native memory (auto-injects MEMORY.md); we point it at the
    # canonical per-agent dir via CLAUDE_COWORK_MEMORY_PATH_OVERRIDE in
    # build_turn_argv, so no system-prompt blurb is needed.
    injects_memory_prompt=False,
    can_fork=True,  # /rewind: HISTORY_REPLAY; /fork: native transcript copy
    fork_prepare=_fork_prepare_replay,
    fork_copy=_fork_copy,
    fork_cleanup=_fork_cleanup,
    login=_OAuthLoginDriver(),
    transcript_codec=_JsonlTranscriptCodec(),
)

register(Harness(CLAUDE_CODE))
