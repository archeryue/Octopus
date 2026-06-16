"""The Codex runtime profile.

Everything Codex-specific as data + collaborators behind the `CODEX`
`RuntimeProfile`: rendering a turn into a `codex exec --json` command (MCP
servers as `-c mcp_servers.*` TOML overrides, developer_instructions, the
exec-flags-before-`resume` ordering), normalizing its event stream, the
one-shot call (D2: non-interactive `codex exec`, final agent message text
extracted from the stream), and — wired in Phase 4 — the device-code login
driver. Codex has no transcript codec (handoff/pull unsupported).

Ported faithfully from the former `backends/codex.py`. Shared per-turn
assembly happens upstream in `assembly.py`; `build_turn_argv` only renders
the neutral `TurnContext`.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .events import HarnessCredential, HarnessEvent
from .harness import Harness
from .login import LoginMethod
from .profile import (
    EventParser,
    OneShotContext,
    ParseOutput,
    RuntimeProfile,
    TurnContext,
    WebCapability,
)
from .registry import register

logger = logging.getLogger(__name__)


# Codex variant of the in-app-tools system prompt (same builtins as Claude;
# phrased for Codex's execution model). Injected via
# `-c developer_instructions=...` every turn.
_OCTOPUS_SYSTEM_PROMPT_CODEX = """\
== Octopus in-app tools ==

You have access to extra tools injected by the Octopus controller. They are \
first-class — call them whenever appropriate, not as a fallback.

[1] `mcp__bg__run(command, description?)` — fire-and-forget a shell command \
that runs in the BACKGROUND across turns. Returns a task_id immediately; when \
the bg task finishes, Octopus injects a follow-up turn with the captured \
output. Use it for anything long-running or unbounded — test suites, builds, \
package installs, sleeps, large fetches. Start it, tell the user briefly what \
you started, then end your turn; a new turn arrives with the result \
(prefixed `[bg-task-result]`). Related: `mcp__bg__cancel(task_id)`, \
`mcp__bg__list()`.

[2] `mcp__ask__user(questions)` — ask the user clarification questions and \
BLOCK until they answer. Use it when a real choice depends on the user and \
there isn't an obviously right answer; don't use it for things you can decide \
yourself or verify from the workspace.

[3] `mcp__ask_agent__ask(name, request, files?)` — delegate to another \
Octopus agent by display name. When the user says "ask <name> to …", \
"delegate this to <name>", or "have <name> review …", that is a direct \
call to invoke this tool — don't paraphrase, just call it with a \
self-contained `request` (the other agent never sees this transcript). \
Returns immediately; the other agent's reply arrives later as a follow-up \
turn prefixed `[agent-reply:<name> delegation=<id>]` (or `[agent-question:…]` \
/ `[agent-error:…]`). If a question arrives, answer it via \
`mcp__ask_agent__answer(delegation_id, choice)` when you can, or ask \
the user via `mcp__ask__user` if you can't. For follow-up rounds \
with the same agent on the same line of work (review iterations, \
"apply that same review to file Y"), call `mcp__ask_agent__ask` \
again but pass the PRIOR `delegation_id` (and omit `name`) — the \
same child session is reused so the other agent keeps their \
transcript and doesn't re-read from scratch. Exactly one of (`name`, \
`delegation_id`) must be set. Related: `mcp__ask_agent__cancel`, \
`mcp__ask_agent__list`."""


def _toml_basic_string(value: str) -> str:
    """Render `value` as a TOML basic string for `-c key=<value>` overrides
    (codex parses the value as TOML). Mirrors VM0's quote_toml_basic_string."""
    out = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    out = "".join(c if (ord(c) >= 0x20 or c in "") else f"\\u{ord(c):04x}" for c in out)
    return f'"{out}"'


def _toml_string_array(items: list[str]) -> str:
    return "[" + ", ".join(_toml_basic_string(i) for i in items) + "]"


def _apply_home_dir(env: dict[str, str], credential: HarnessCredential | None) -> None:
    """Apply a home_dir credential. CODEX_HOME isolates the subscription
    login (auth.json) and is where codex persists its own token refresh; a
    per-credential dir overrides the host default ~/.codex."""
    if credential is not None and credential.home_dir:
        env["CODEX_HOME"] = credential.home_dir


def _mcp_config_args(ctx: TurnContext) -> list[str]:
    """`-c mcp_servers.<key>.*` overrides for the assembled MCP servers. We
    use per-invocation `-c` overrides (not a config.toml) so per-session
    callback env stays per-session while CODEX_HOME remains the stable
    per-credential auth dir."""
    args: list[str] = []
    for e in ctx.mcp_servers:
        base = f"mcp_servers.{e.key}"
        args += ["-c", f"{base}.command={_toml_basic_string(e.command)}"]
        args += ["-c", f"{base}.args={_toml_string_array(e.args)}"]
        for env_key, env_val in e.env.items():
            args += ["-c", f"{base}.env.{env_key}={_toml_basic_string(env_val)}"]
    return args


# ------------------------------------------------------------------ turn argv


def build_turn_argv(ctx: TurnContext) -> tuple[list[str], dict[str, Any]]:
    """Render a `codex exec --json` command for one turn.

    Exec-level flags MUST precede the `resume` subcommand (resume's parser
    accepts neither -C nor the sandbox flag); `--` precedes the prompt."""
    argv: list[str] = [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-C",
        ctx.working_dir,
        "-c",
        f"developer_instructions={_toml_basic_string(ctx.system_prompt)}",
    ]
    if ctx.web_research:
        # A research leaf: enable codex's native web_search and run READ-ONLY so
        # it can search the web but can't write/exec on the host — codex's
        # honest equivalent of Claude's web-leaf denylist (no per-tool allowlist
        # exists). native-deep-research.md §4.
        argv += ["--sandbox", "read-only", "-c", "tools.web_search=true"]
    else:
        # Analog of Claude's --dangerously-skip-permissions for a full turn.
        argv += ["--dangerously-bypass-approvals-and-sandbox"]
    argv += _mcp_config_args(ctx)
    if ctx.model:
        argv += ["-m", ctx.model]
    if ctx.resume_id:
        argv += ["resume", ctx.resume_id, "--", ctx.prompt]
    else:
        argv += ["--", ctx.prompt]

    env = os.environ.copy()
    _apply_home_dir(env, ctx.credential)
    return argv, {"cwd": ctx.working_dir, "env": env}


# ------------------------------------------------------------------ event parsing


def _mcp_result_text(result: Any) -> str:
    """Flatten an MCP tool result into display text. Confirmed shape:
    `{content:[{type:"text",text:...}], structured_content:{result:...}}`."""
    if result is None:
        return ""
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts = [
                c.get("text", "")
                for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            ]
            if parts:
                return "\n".join(parts)
        sc = result.get("structured_content")
        if isinstance(sc, dict) and "result" in sc:
            return str(sc["result"])
    return str(result)


class CodexEventParser(EventParser):
    """Normalize `codex exec --json` into HarnessEvents. Holds the captured
    thread id (the resume id), surfaced early on `session_started`."""

    def __init__(self) -> None:
        self._captured_thread_id: str | None = None

    def parse(self, obj: dict[str, Any]) -> ParseOutput:
        kind = obj.get("type")

        if kind == "thread.started":
            tid = obj.get("thread_id")
            self._captured_thread_id = tid
            if tid:
                return ParseOutput(
                    events=[HarnessEvent(type="session_started", session_id=tid)]
                )
            return ParseOutput()

        if kind == "turn.started":
            return ParseOutput()

        if kind == "turn.completed":
            usage = obj.get("usage") or {}
            return ParseOutput(
                events=[
                    HarnessEvent(
                        type="result",
                        session_id=self._captured_thread_id,
                        cost=None,  # Codex reports tokens, not USD
                        num_turns=1,
                        raw={"usage": usage},
                    )
                ],
                end_of_stream=True,
            )

        if kind == "turn.failed":
            err = obj.get("error")
            msg = err if isinstance(err, str) else (
                (err or {}).get("message") if isinstance(err, dict) else None
            )
            return ParseOutput(
                events=[
                    HarnessEvent(
                        type="result",
                        session_id=self._captured_thread_id,
                        is_error=True,
                        content=msg or "Turn failed",
                        raw=obj,
                    )
                ],
                end_of_stream=True,
            )

        if kind == "error":
            return ParseOutput(
                events=[
                    HarnessEvent(
                        type="error",
                        is_error=True,
                        content=obj.get("message") or obj.get("error") or "Unknown error",
                        raw=obj,
                    )
                ]
            )

        if isinstance(kind, str) and kind.startswith("item."):
            return ParseOutput(events=self._item_events(kind, obj))

        logger.debug("Unhandled codex event type: %s", kind)
        return ParseOutput()

    def _item_events(self, kind: str, obj: dict[str, Any]) -> list[HarnessEvent]:
        item = obj.get("item")
        if not isinstance(item, dict):
            return []
        item_type = item.get("type")
        item_id = item.get("id")
        started = kind == "item.started"
        completed = kind == "item.completed"

        if item_type == "agent_message":
            text = item.get("text")
            if completed and text:
                return [HarnessEvent(type="text", content=text, raw=item)]
            return []

        if item_type == "reasoning":
            text = item.get("text")
            if completed and text:
                return [HarnessEvent(type="thinking", content=text, raw=item)]
            return []

        if item_type == "mcp_tool_call":
            server = item.get("server") or ""
            tool = item.get("tool") or ""
            tool_name = f"mcp__{server}__{tool}"
            if started:
                return [
                    HarnessEvent(
                        type="tool_use",
                        tool_name=tool_name,
                        tool_use_id=item_id,
                        tool_input=item.get("arguments") or {},
                        raw=item,
                    )
                ]
            if completed:
                err = item.get("error")
                return [
                    HarnessEvent(
                        type="tool_result",
                        tool_use_id=item_id,
                        content=(str(err) if err is not None else _mcp_result_text(item.get("result"))),
                        is_error=err is not None,
                        raw=item,
                    )
                ]
            return []

        if item_type == "command_execution":
            if started and item.get("command") is not None:
                return [
                    HarnessEvent(
                        type="tool_use",
                        tool_name="Bash",
                        tool_use_id=item_id,
                        tool_input={"command": item.get("command")},
                        raw=item,
                    )
                ]
            if completed:
                output = item.get("aggregated_output")
                if output is None:
                    output = item.get("output") or ""
                return [
                    HarnessEvent(
                        type="tool_result",
                        tool_use_id=item_id,
                        content=output,
                        is_error=item.get("exit_code") not in (0, None),
                        raw=item,
                    )
                ]
            return []

        if item_type in ("file_edit", "file_write", "file_read"):
            tool = {"file_edit": "Edit", "file_write": "Write", "file_read": "Read"}[item_type]
            if started and item.get("path") is not None:
                return [
                    HarnessEvent(
                        type="tool_use",
                        tool_name=tool,
                        tool_use_id=item_id,
                        tool_input={"file_path": item.get("path")},
                        raw=item,
                    )
                ]
            if completed:
                return [
                    HarnessEvent(
                        type="tool_result",
                        tool_use_id=item_id,
                        content=item.get("diff") or "File operation completed",
                        is_error=False,
                        raw=item,
                    )
                ]
            return []

        if item_type == "file_change":
            changes = item.get("changes")
            if kind == "item.completed" and isinstance(changes, list) and changes:
                summary = "\n".join(
                    f"{c.get('kind', 'change')}: {c.get('path', '')}" for c in changes
                )
                return [HarnessEvent(type="text", content=summary, raw=item)]
            return []

        return []


# ------------------------------------------------------------------ one-shot (D2)


def build_oneshot_argv(ctx: OneShotContext) -> tuple[list[str], dict[str, Any]]:
    """Lean, non-interactive `codex exec --json` — no MCP, no developer
    instructions, no connectors. The JSON stream's final agent_message is the
    result (extracted by parse_oneshot_stdout)."""
    argv = [
        "codex",
        "exec",
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
    ]
    if ctx.working_dir:
        argv += ["-C", ctx.working_dir]
    if ctx.model:
        argv += ["-m", ctx.model]
    argv += ["--", ctx.prompt]
    env = os.environ.copy()
    _apply_home_dir(env, ctx.credential)
    return argv, {"cwd": ctx.working_dir or os.getcwd(), "env": env}


def parse_oneshot_stdout(stdout: str) -> str:
    """Concatenate the agent_message text(s) from the codex event stream."""
    texts: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "item.completed":
            item = obj.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message" and item.get("text"):
                texts.append(item["text"])
    return "\n".join(texts)


# ------------------------------------------------------------------ fork (HISTORY_REPLAY)


async def _fork_prepare_replay(
    messages: list[Any],
    working_dir: str,
    resume_id_hint: str | None,
    fork_id: str,
) -> "Any":
    """No on-disk work (session-tree-rewind.md §5.3.2). Codex's resume state is
    internal to the binary and has no transcript codec, so the first fork turn
    instead carries the truncated history wrapped into its USER PROMPT (done in
    `SessionManager.send_message`). `thread.started` captures the real resume id
    on turn 1; turn 2+ uses native Codex resume. The `resume_id_hint` is
    ignored."""
    from .fork import ForkArtifact

    return ForkArtifact(resume_id=None, needs_replay=True)


def _codex_sessions_dir(credential: Any) -> "Path":
    """CODEX_HOME/sessions — codex's rollout store (date-partitioned files named
    ``rollout-<ts>-<id>.jsonl``). The id is cwd-independent, so a copy resumes
    from anywhere. Uses the credential's CODEX_HOME, else the host default
    ~/.codex."""
    from pathlib import Path

    home = (
        credential.home_dir
        if credential is not None and getattr(credential, "home_dir", None)
        else os.path.join(os.path.expanduser("~"), ".codex")
    )
    return Path(home) / "sessions"


def _rollout_meta_id(p: "Path") -> "str | None":
    """The `session_meta.payload.id` of a rollout (its first line), or None."""
    try:
        with p.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                if d.get("type") == "session_meta":
                    return (d.get("payload") or {}).get("id")
                return None  # session_meta is the first record
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _find_rollout(sessions_dir: "Path", resume_id: str) -> "Path | None":
    """Locate the rollout whose `session_meta.id` == resume_id. The filename
    usually embeds the id, but since this also drives DELETION we CONFIRM against
    session_meta before returning a match (Vera review) — never delete on a mere
    filename-substring hit."""
    if not sessions_dir.is_dir():
        return None
    # Fast path: filename contains the id — confirm before trusting it.
    for p in sessions_dir.rglob(f"*{resume_id}*.jsonl"):
        if _rollout_meta_id(p) == resume_id:
            return p
    # Fallback: scan every rollout's session_meta.
    for p in sessions_dir.rglob("rollout-*.jsonl"):
        if _rollout_meta_id(p) == resume_id:
            return p
    return None


async def _fork_copy(
    *,
    parent_working_dir: str,
    parent_resume_id: str | None,
    parent_credential: Any = None,
    dest_working_dir: str,  # unused: codex rollouts are keyed by id, not cwd
    new_resume_id: str,
) -> "Any":
    """Full-copy fork (session-fork-copy.md): copy the parent's rollout to a new
    rollout file under the SAME CODEX_HOME with `session_meta.id` rewritten to
    `new_resume_id`, so the fork resumes the whole conversation natively — no
    history replay. Codex resume is by id (cwd-independent), so the copied
    working dir is irrelevant here. Falls back to replay when the parent has no
    rollout yet (never ran a turn)."""
    from .fork import ForkArtifact

    if not parent_resume_id:
        return ForkArtifact(resume_id=None, needs_replay=True)
    sessions = _codex_sessions_dir(parent_credential)
    src = _find_rollout(sessions, parent_resume_id)
    if src is None:
        return ForkArtifact(resume_id=None, needs_replay=True)

    dest = src.parent / f"rollout-fork-{new_resume_id}.jsonl"
    # Temp + atomic rename so a crash mid-copy can't leave a half-written
    # rollout that later looks resumable (Vera review).
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
                if d.get("type") == "session_meta" and isinstance(d.get("payload"), dict):
                    if d["payload"].get("id") == parent_resume_id:
                        d["payload"]["id"] = new_resume_id
                fout.write(json.dumps(d) + "\n")
        os.replace(tmp, dest)
    except Exception:
        tmp.unlink(missing_ok=True)  # don't leave partial temp junk behind
        raise
    return ForkArtifact(resume_id=new_resume_id, needs_replay=False)


async def _fork_cleanup(
    working_dir: str,  # unused: rollouts live under CODEX_HOME, not the cwd
    resume_id_hint: str | None,
    fork_id: str,
    *,
    credential: Any = None,
) -> None:
    """Remove a copied rollout left by an incomplete full-copy saga. Rollouts
    live under CODEX_HOME (not the working dir), so this needs the credential to
    locate the store. No-op for /rewind (replay copies no rollout — nothing
    named by the hint exists)."""
    if not resume_id_hint:
        return
    sessions = _codex_sessions_dir(credential)
    # Target the COPY's deterministic name (`_fork_copy` wrote
    # rollout-fork-<id>.jsonl) directly — NOT the read-based `_find_rollout`,
    # whose session_meta read could fail and make a present-but-unreadable
    # rollout look absent, so cleanup "succeeds" and the row is deleted, leaking
    # it (Vera review). A name match needs no read.
    if not sessions.is_dir():
        return
    for copied in sessions.rglob(f"rollout-fork-{resume_id_hint}.jsonl"):
        try:
            copied.unlink()
        except FileNotFoundError:
            pass  # already gone — idempotent
        except OSError:
            # RE-RAISE: a returning cleanup is read as success and the caller
            # then deletes the DB row, which would strand this rollout. Keep the
            # row by surfacing the failure for a later retry.
            logger.exception(
                "fork %s: failed to remove copied rollout %s", fork_id, copied
            )
            raise


# ------------------------------------------------------------------ login driver


class _DeviceLoginDriver:
    """Codex's device-code login, wrapping the CodexLoginManager singleton.
    `codex login --device-auth` runs against a per-credential CODEX_HOME; the
    UI polls `get()` for the verification URL + code and for success. Owns the
    on-disk cleanup (CODEX_HOME) so the credentials router doesn't branch on
    kind."""

    method = LoginMethod.device_code

    async def start(
        self, label: str | None = None, *, reauth_credential_id: str | None = None
    ):
        from ..codex_login import codex_login_manager

        return await codex_login_manager.start(
            (label or "").strip(), reauth_credential_id=reauth_credential_id
        )

    async def submit_code(self, login_id: str, code: str):
        raise NotImplementedError("device_code login polls; it has no code to submit")

    def get(self, login_id: str):
        from ..codex_login import codex_login_manager

        return codex_login_manager.get(login_id)

    async def cancel(self, login_id: str) -> None:
        from ..codex_login import codex_login_manager

        await codex_login_manager.cancel(login_id)

    def cleanup_credential(self, credential_id: str) -> None:
        # A Codex credential IS its CODEX_HOME dir (auth.json + token) — remove
        # it so deletion actually revokes local access.
        import shutil

        from ..codex_login import codex_home_for

        shutil.rmtree(codex_home_for(credential_id), ignore_errors=True)


# ------------------------------------------------------------------ profile


# Phrases Codex / the OpenAI backend emit when the stored ChatGPT auth has
# expired or been revoked (harness-credential-reauth.md §3). Codex surfaces
# these in `turn.failed` / `error` event text; we also scan its stderr.
_CODEX_AUTH_ERROR_PATTERNS = (
    "401 unauthorized",
    "error 401",
    "invalid authentication credentials",
    "invalid api key",
    "incorrect api key",
    "your authentication token has expired",
    "please run `codex login`",
    "please run codex login",
    "not logged in",
    # NB: deliberately NOT a bare "unauthorized" / "token has expired" — those
    # appear in non-auth failures (an MCP/connector 401, a tool's "GitHub
    # Unauthorized") that escalate to a Codex `error`/`turn.failed`, and would
    # wrongly flag the harness credential dead. Patterns must be auth-specific.
)

# Transient provider-reliability failures worth an automatic retry
# (harness-transient-retry.md §3). Server-side 5xx / overload / dropped
# connection only — NO "rate limit" / "429" / "quota" (the user's limit) and
# no auth phrases (handled above).
_CODEX_TRANSIENT_ERROR_PATTERNS = (
    "overloaded",
    "error 500",
    "error 502",
    "error 503",
    "error 504",
    "internal server error",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "temporarily unavailable",
    "connection error",
    "connection reset",
    "connection refused",
    "stream error",
    "stream disconnected",
    "request timed out",
    "timed out",
    # Server-side throttle (NOT the user's usage limit) — retryable.
    "temporarily limiting requests",
    "not your usage limit",
)


CODEX = RuntimeProfile(
    backend="codex",
    binary="codex",
    tools_prompt=_OCTOPUS_SYSTEM_PROMPT_CODEX,
    credential_style="home_dir",
    premature_exit_recovery=False,
    auth_error_patterns=_CODEX_AUTH_ERROR_PATTERNS,
    transient_error_patterns=_CODEX_TRANSIENT_ERROR_PATTERNS,
    # Single combined search-and-read tool, enabled per-leaf via the
    # web_research render path (-c tools.web_search=true). native-deep-research.md §4.
    web=WebCapability(tool_names=("web_search",), combined=True),
    close_stdin_after_start=True,
    build_turn_argv=build_turn_argv,
    new_event_parser=CodexEventParser,
    build_oneshot_argv=build_oneshot_argv,
    parse_oneshot_stdout=parse_oneshot_stdout,
    # Codex has no usable native memory in exec, so it reads/writes the shared
    # per-agent markdown dir with file tools, driven by the injected blurb
    # (docs/plans/memory.md §3). Memory is decoupled from CODEX_HOME; the
    # canonical dir is ensured by session_manager.
    injects_memory_prompt=True,
    can_fork=True,  # /rewind: HISTORY_REPLAY; /fork: native rollout copy
    fork_prepare=_fork_prepare_replay,
    fork_copy=_fork_copy,
    fork_cleanup=_fork_cleanup,
    login=_DeviceLoginDriver(),
    transcript_codec=None,  # Codex has no handoff/pull transcript format
)

register(Harness(CODEX))
