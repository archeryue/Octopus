"""Codex backend — talks directly to the `codex` CLI subprocess.

A Codex session is a Claude session that happens to spawn a different binary
(codex-backend.md §0): same chat UX, same in-app MCP tools, same schedules /
bridges / archive. Only four things change — which CLI we spawn, how we
translate its `exec --json` stream into `BackendEvent`, how we inject our MCP
tools + instructions, and how we authenticate.

Grounding:
- The `exec` flag surface is read off the real `codex` 0.132.0 `--help`
  (`--json`, `--dangerously-bypass-approvals-and-sandbox`, `--skip-git-repo-check`,
  `-C <dir>`, `-c key=value`, `-m <model>`, `resume <id>`).
- The arg ORDER (exec flags before the `resume` subcommand; `--` before the
  prompt) and the event→type mapping come from VM0's shipped Codex support
  (`crates/guest-agent/.../command.rs` `build_codex_args`,
  `turbo/apps/cli/.../codex-event-parser.ts`).

What still needs a live, logged-in run to confirm (codex-backend.md §12, Phase
C — requires the user's ChatGPT subscription, which we cannot exercise here):
the exact `--json` field names, that `codex exec` honors `-c mcp_servers.*`
overrides and the tool name it exposes to the model, and the login flow. The
event NORMALIZER below is exercised against a scripted fake CLI
(`tests/_fixtures/fake_codex_cli.py`); `build_args` is asserted structurally.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from .base import BackendCredential, BackendEvent
from .subprocess_jsonl import SubprocessJsonlBackend

logger = logging.getLogger(__name__)

_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)


# Codex variant of the in-app-tools system prompt. The tool descriptions are
# the same three servers as Claude (viewer / bg / ask); only the execution
# model differs — Codex has no "Claude Code harness auto-backgrounding Bash"
# behavior, so the bg-vs-shell guidance is phrased for Codex (codex-backend.md
# §5.4). Injected via `-c developer_instructions=...` on every turn.
_OCTOPUS_SYSTEM_PROMPT_CODEX = """\
== Octopus in-app tools ==

You have access to extra tools injected by the Octopus controller. They are \
first-class — call them whenever appropriate, not as a fallback.

[1] `mcp__viewer__show_file` — opens a file from the current working \
directory in an in-app viewer modal so the user can see it directly. Call it \
when the user types `/showme <path>`, or proactively when showing a file is \
clearer than quoting it. After calling it, briefly say what you opened — \
don't paste the file contents.

[2] `mcp__bg__run(command, description?)` — fire-and-forget a shell command \
that runs in the BACKGROUND across turns. Returns a task_id immediately; when \
the bg task finishes, Octopus injects a follow-up turn with the captured \
output. Use it for anything long-running or unbounded — test suites, builds, \
package installs, sleeps, large fetches. Start it, tell the user briefly what \
you started, then end your turn; a new turn arrives with the result \
(prefixed `[bg-task-result]`). Related: `mcp__bg__cancel(task_id)`, \
`mcp__bg__list()`.

[3] `mcp__ask__user(questions)` — ask the user clarification questions and \
BLOCK until they answer. Use it when a real choice depends on the user and \
there isn't an obviously right answer; don't use it for things you can decide \
yourself or verify from the workspace."""


def _toml_basic_string(value: str) -> str:
    """Render `value` as a TOML basic string (the `"..."` form), escaping the
    characters TOML requires. Used for `-c key=<value>` overrides, whose value
    is parsed as TOML (mirrors VM0's `quote_toml_basic_string`)."""
    out = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    # Remaining control chars → \uXXXX (TOML forbids raw control chars).
    out = "".join(
        c if (ord(c) >= 0x20 or c in "") else f"\\u{ord(c):04x}" for c in out
    )
    return f'"{out}"'


def _toml_string_array(items: list[str]) -> str:
    return "[" + ", ".join(_toml_basic_string(i) for i in items) + "]"


class CodexBackend(SubprocessJsonlBackend):
    """Spawn `codex exec --json` and translate its event stream into
    BackendEvents.

    Lifecycle is identical to Claude — one CLI invocation per turn, `result`
    ends it, `_close_stream` releases the iterator — so no
    SubprocessJsonlBackend changes are needed. Codex does NOT use the
    Claude-CLI premature-exit recovery (`wants_premature_exit_recovery` stays
    False), so a turn runs exactly once.
    """

    name = "codex"
    binary = "codex"
    wants_premature_exit_recovery = False

    def __init__(
        self,
        session_id: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        mcp_servers: list[str] | None = None,
        credential_home: str | None = None,
    ) -> None:
        super().__init__()
        self._session_id = session_id
        self._model = model
        # Agent persona, prepended to the Octopus tools section in
        # developer_instructions. None = just the tools section.
        self._agent_system_prompt = system_prompt
        # Subset of {"viewer","bg","ask"} to register; None = all three.
        self._mcp_servers = mcp_servers
        # Per-credential CODEX_HOME (holds auth.json + token refresh). None
        # inherits the host's default ~/.codex (option A — host `codex login`,
        # codex-backend.md §7).
        self._credential_home = credential_home
        self._captured_thread_id: str | None = None

    # ------------------------------------------------------------------ build

    def _mcp_config_args(self, absolute_working_dir: str) -> list[str]:
        """`-c mcp_servers.<key>.*` overrides registering our three stdio MCP
        servers (codex-backend.md §5.3). We use per-invocation `-c` overrides
        rather than a config.toml file so the per-session callback env
        (OCTOPUS_SESSION_ID) stays per-session while CODEX_HOME remains the
        stable per-credential auth dir (token refresh persists there)."""
        from ..config import settings as _settings

        api_base = f"http://127.0.0.1:{_settings.port}"
        callback_env = {
            "OCTOPUS_API_BASE": api_base,
            "OCTOPUS_AUTH_TOKEN": _settings.auth_token,
            "PYTHONPATH": _REPO_ROOT,
        }
        if self._session_id:
            callback_env["OCTOPUS_SESSION_ID"] = self._session_id

        servers: dict[str, dict[str, Any]] = {
            "viewer": {
                "module": "server.mcp_servers.viewer",
                "env": {
                    "OCTOPUS_WORKING_DIR": absolute_working_dir,
                    "PYTHONPATH": _REPO_ROOT,
                },
            },
            "bg": {"module": "server.mcp_servers.bg", "env": callback_env},
            "ask": {"module": "server.mcp_servers.ask", "env": callback_env},
        }
        selected = (
            {k: v for k, v in servers.items() if k in self._mcp_servers}
            if self._mcp_servers is not None
            else servers
        )

        args: list[str] = []
        for key, spec in selected.items():
            base = f"mcp_servers.{key}"
            args += ["-c", f"{base}.command={_toml_basic_string(sys.executable)}"]
            args += [
                "-c",
                f"{base}.args={_toml_string_array(['-m', spec['module']])}",
            ]
            for env_key, env_val in spec["env"].items():
                args += [
                    "-c",
                    f"{base}.env.{env_key}={_toml_basic_string(env_val)}",
                ]
        return args

    def _developer_instructions(self) -> str:
        if self._agent_system_prompt:
            return f"{self._agent_system_prompt}\n\n{_OCTOPUS_SYSTEM_PROMPT_CODEX}"
        return _OCTOPUS_SYSTEM_PROMPT_CODEX

    def build_args(
        self,
        prompt: str,
        working_dir: str,
        resume_id: str | None,
        credential: BackendCredential | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        # Resolve to an absolute path first — same reasoning as the Claude
        # backend (MCP-server grandchildren inherit cwd).
        absolute_working_dir = str(Path(working_dir).resolve())

        # Exec-level flags MUST precede the `resume` subcommand (resume's own
        # parser accepts neither -C nor the sandbox flag) — VM0 build_codex_args.
        argv: list[str] = [
            self.binary,
            "exec",
            "--json",
            # Direct analog of Claude's --dangerously-skip-permissions: Octopus
            # is the only thing spawning this on the trusting user's behalf,
            # and we're not inside an outer sandbox (codex-backend.md §5.6).
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-C",
            absolute_working_dir,
            "-c",
            f"developer_instructions={_toml_basic_string(self._developer_instructions())}",
        ]
        argv += self._mcp_config_args(absolute_working_dir)
        if self._model:
            argv += ["-m", self._model]
        # New turn: `… -- <prompt>`. Resume: `… resume <id> -- <prompt>`.
        if resume_id:
            argv += ["resume", resume_id, "--", prompt]
        else:
            argv += ["--", prompt]

        env = os.environ.copy()
        # CODEX_HOME isolates the subscription login (auth.json) and is where
        # codex persists its own token refresh. A per-credential dir overrides
        # the host default; None inherits ~/.codex (host `codex login`).
        if self._credential_home:
            env["CODEX_HOME"] = self._credential_home
        return argv, {"cwd": absolute_working_dir, "env": env}

    async def send_initial_prompt(self, prompt: str) -> None:
        """Close stdin so codex proceeds with the positional-argv prompt.

        Unlike `claude --print`, `codex exec` *reads stdin* even when a prompt
        is passed positionally (it appends piped stdin as a `<stdin>` block —
        see `codex exec --help`). With our stdin held open as a pipe it blocks
        forever on "Reading additional input from stdin…" and never runs the
        turn. Closing stdin gives it EOF immediately so it uses just the argv
        prompt. (Verified against codex 0.132.0, Phase C.)
        """
        proc = self._process
        if proc is not None and proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:
                logger.debug("codex: closing stdin failed", exc_info=True)

    # ------------------------------------------------------------------ parse

    async def on_stdout_line(self, line: str) -> None:
        obj = self.parse_json_line(line)
        if obj is None:
            return
        kind = obj.get("type")

        if kind == "thread.started":
            # Emit early so the resume id (thread_id) is captured before
            # `result`, mirroring the Claude `system/init` handling.
            tid = obj.get("thread_id")
            self._captured_thread_id = tid
            if tid:
                self._emit(BackendEvent(type="session_started", session_id=tid))
            return

        if kind == "turn.started":
            return

        if kind == "turn.completed":
            usage = obj.get("usage") or {}
            self._emit(
                BackendEvent(
                    type="result",
                    session_id=self._captured_thread_id,
                    # Codex reports tokens, not USD — leave cost unset.
                    cost=None,
                    num_turns=1,
                    raw={"usage": usage},
                )
            )
            self._close_stream()
            return

        if kind == "turn.failed":
            err = obj.get("error")
            msg = err if isinstance(err, str) else (
                (err or {}).get("message") if isinstance(err, dict) else None
            )
            self._emit(
                BackendEvent(
                    type="result",
                    session_id=self._captured_thread_id,
                    is_error=True,
                    content=msg or "Turn failed",
                    raw=obj,
                )
            )
            self._close_stream()
            return

        if kind == "error":
            self._emit(
                BackendEvent(
                    type="error",
                    is_error=True,
                    content=obj.get("message") or obj.get("error") or "Unknown error",
                    raw=obj,
                )
            )
            return

        if isinstance(kind, str) and kind.startswith("item."):
            self._handle_item_event(kind, obj)
            return

        logger.debug("Unhandled codex event type: %s", kind)

    @staticmethod
    def _mcp_result_text(result: Any) -> str:
        """Flatten an MCP tool result into display text. The confirmed shape is
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

    def _handle_item_event(self, kind: str, obj: dict[str, Any]) -> None:
        item = obj.get("item")
        if not isinstance(item, dict):
            return
        item_type = item.get("type")
        item_id = item.get("id")
        started = kind == "item.started"
        completed = kind == "item.completed"

        if item_type == "agent_message":
            text = item.get("text")
            if completed and text:
                self._emit(BackendEvent(type="text", content=text, raw=item))
            return

        if item_type == "reasoning":
            text = item.get("text")
            if completed and text:
                self._emit(BackendEvent(type="thinking", content=text, raw=item))
            return

        if item_type == "mcp_tool_call":
            # Confirmed shape on codex 0.132.0 (Phase C live capture):
            #   {type:"mcp_tool_call", server, tool, arguments, result, error, status}
            # Emit `mcp__<server>__<tool>` so the name matches Claude's scheme
            # and our developer_instructions — and so the frontend's
            # mcp__viewer__show_file viewer-dialog trigger fires for Codex too.
            server = item.get("server") or ""
            tool = item.get("tool") or ""
            tool_name = f"mcp__{server}__{tool}"
            if started:
                self._emit(
                    BackendEvent(
                        type="tool_use",
                        tool_name=tool_name,
                        tool_use_id=item_id,
                        tool_input=item.get("arguments") or {},
                        raw=item,
                    )
                )
            elif completed:
                err = item.get("error")
                self._emit(
                    BackendEvent(
                        type="tool_result",
                        tool_use_id=item_id,
                        content=(
                            str(err)
                            if err is not None
                            else self._mcp_result_text(item.get("result"))
                        ),
                        is_error=err is not None,
                        raw=item,
                    )
                )
            return

        if item_type == "command_execution":
            if started and item.get("command") is not None:
                self._emit(
                    BackendEvent(
                        type="tool_use",
                        tool_name="Bash",
                        tool_use_id=item_id,
                        tool_input={"command": item.get("command")},
                        raw=item,
                    )
                )
            elif completed:
                output = item.get("aggregated_output")
                if output is None:
                    output = item.get("output") or ""
                self._emit(
                    BackendEvent(
                        type="tool_result",
                        tool_use_id=item_id,
                        content=output,
                        is_error=item.get("exit_code") not in (0, None),
                        raw=item,
                    )
                )
            return

        if item_type in ("file_edit", "file_write", "file_read"):
            tool = {
                "file_edit": "Edit",
                "file_write": "Write",
                "file_read": "Read",
            }[item_type]
            if started and item.get("path") is not None:
                self._emit(
                    BackendEvent(
                        type="tool_use",
                        tool_name=tool,
                        tool_use_id=item_id,
                        tool_input={"file_path": item.get("path")},
                        raw=item,
                    )
                )
            elif completed:
                self._emit(
                    BackendEvent(
                        type="tool_result",
                        tool_use_id=item_id,
                        content=item.get("diff") or "File operation completed",
                        is_error=False,
                        raw=item,
                    )
                )
            return

        if item_type == "file_change":
            changes = item.get("changes")
            if completed and isinstance(changes, list) and changes:
                summary = "\n".join(
                    f"{c.get('kind', 'change')}: {c.get('path', '')}" for c in changes
                )
                self._emit(BackendEvent(type="text", content=summary, raw=item))
            return
