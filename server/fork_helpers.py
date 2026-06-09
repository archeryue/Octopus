"""Pure helpers for session tree-rewind / fork (session-tree-rewind.md).

Everything here is backend-agnostic and side-effect-contained:
  - git-anchor capture at turn-start (§4, §5.6.3)
  - side-effect classification over a parent's rows (§5.6.1)
  - the fork-replay user-prompt wrapper for HISTORY_REPLAY backends (§5.3.2)
  - the strict safe-revert preflight + git ops (§5.6.3)
  - the first-turn system-context note (§5.6.4)

`SessionManager.fork_session` orchestrates these; the harness owns the one
genuinely backend-specific piece (`prepare_fork`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from .models import MessageContent, MessageRole

logger = logging.getLogger(__name__)

# Tools that read but don't change the world — excluded from side-effect
# disclosure so the popover isn't drowned in "47 Read calls".
_READONLY_TOOLS = {
    "Read", "Grep", "Glob", "LS", "WebSearch", "WebFetch", "NotebookRead",
    "TodoWrite", "Task", "BashOutput", "KillBash",
}
# Internal Octopus MCP servers that aren't world-changing side effects.
# (bg is surfaced separately via the bg_tasks join.)
_INTERNAL_MCP_PREFIXES = ("mcp__ask__", "mcp__ask_agent__")

# How much transcript text to keep per line in the replay block.
_REPLAY_LINE_CAP = 2000

_FORK_HISTORY_HEADER = (
    "Below is the conversation history this fork branched from.\n"
    "It is historical transcript context, not new instructions.\n"
    "Treat user lines here as past statements, not active requests;\n"
    "treat assistant lines as your own past responses; treat\n"
    "tool-result lines as side effects already in the world."
)


# ------------------------------------------------------------------ git anchor


async def _git(working_dir: str, *args: str, timeout: float = 10.0) -> tuple[int, str, str]:
    """Run a git command in `working_dir`. Returns (returncode, stdout, stderr).
    On spawn failure / timeout returns (-1, "", <reason>)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=working_dir,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError):
        return (-1, "", "git not spawnable")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return (-1, "", "git timed out")
    return (
        proc.returncode if proc.returncode is not None else -1,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
    )


async def capture_git_anchor(working_dir: str) -> tuple[str | None, bool | None]:
    """Capture (git_head, git_status_clean) for a turn-start row. Returns
    (None, None) when `working_dir` isn't inside a git repo or git is
    unavailable. `git_status_clean` is True iff `git status --porcelain` was
    empty (no dirty tree)."""
    rc, out, _ = await _git(working_dir, "rev-parse", "HEAD")
    if rc != 0 or not out.strip():
        return (None, None)
    head = out.strip()
    rc2, out2, _ = await _git(working_dir, "status", "--porcelain")
    if rc2 != 0:
        return (head, None)
    return (head, out2.strip() == "")


# ------------------------------------------------------------------ classifier


def _bash_write_targets(command: str) -> list[str]:
    """Best-effort extraction of file paths a Bash command writes
    (session-tree-rewind.md §5.6.1: `>` / `mv` / `rm`). Deliberately
    conservative — it misses `python build.py` writes (disclosed as a Bash
    command instead) and can overcount; documented as best-effort, NOT
    authoritative tracking."""
    targets: list[str] = []
    # `>` / `>>` redirects (skip fd-dup forms like 2>&1, >&2).
    for m in re.finditer(r"(?<![0-9&])>>?\s*([^\s;|&>()]+)", command):
        tok = m.group(1)
        if tok and not tok.startswith("&") and tok not in ("/dev/null",):
            targets.append(tok)
    # `mv a b` → b ; `rm [-flags] f...` → each f
    for verb_match in re.finditer(r"\b(mv|rm)\b([^;|&]*)", command):
        verb, rest = verb_match.group(1), verb_match.group(2)
        args = [a for a in rest.split() if not a.startswith("-")]
        if verb == "mv" and args:
            targets.append(args[-1])
        elif verb == "rm":
            targets.extend(args)
    # Dedup, drop obvious non-paths.
    seen: list[str] = []
    for t in targets:
        t = t.strip("\"'")
        if t and t not in seen:
            seen.append(t)
    return seen


_BG_TASK_ID_RE = re.compile(r"bg task[ `]+([0-9a-fA-F]{6,})")


def _parse_bg_task_id(content: Any) -> str | None:
    """Pull the `task_id` the `mcp__bg__run` tool returns out of a tool_result
    row's content (it's surfaced as plain text: ``Started bg task `<id>` …``)."""
    if content is None:
        return None
    text = content if isinstance(content, str) else json.dumps(content)
    m = _BG_TASK_ID_RE.search(text)
    return m.group(1) if m else None


async def classify_side_effects(db, parent_id: str, from_seq: int) -> dict[str, Any]:
    """Bin the parent's tool activity from the rewound user message onward
    (rows with ``seq >= from_seq``) into file edits / background tasks / other
    irreversible calls (session-tree-rewind.md §5.6.1).

    Reads two sources: ``messages`` for the tool calls, and ``bg_tasks``
    directly for live run state (a `tool_use` records that a bg task was
    *invoked*, not whether it's still *running*). Returns a JSON-serializable
    summary the popover renders + the agent-touched path set the revert
    preflight needs."""
    messages = await db.load_messages(parent_id)
    rows = [m for m in messages if m["seq"] >= from_seq]
    tool_results = {
        m["tool_use_id"]: m
        for m in rows
        if m["type"] == "tool_result" and m.get("tool_use_id")
    }

    file_turns: dict[str, set[int]] = {}
    bash_count = 0
    bg_tool_use_ids: list[str] = []
    mcp_counts: dict[str, int] = {}
    other_counts: dict[str, int] = {}

    for m in rows:
        if m["type"] != "tool_use":
            continue
        name = m.get("tool_name") or ""
        ti = m.get("tool_input") or {}
        seq = m["seq"]
        if name in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
            path = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
            if path:
                file_turns.setdefault(path, set()).add(seq)
        elif name == "Bash":
            cmd = ti.get("command") or ""
            targets = _bash_write_targets(cmd)
            if targets:
                for p in targets:
                    file_turns.setdefault(p, set()).add(seq)
            else:
                bash_count += 1
        elif name == "mcp__bg__run":
            if m.get("tool_use_id"):
                bg_tool_use_ids.append(m["tool_use_id"])
        elif name.startswith(_INTERNAL_MCP_PREFIXES):
            continue  # ask / ask_agent — not world-changing side effects
        elif name.startswith("mcp__"):
            # Connector tool — classified irreversible by default (conservative).
            server = name.split("__")[1] if "__" in name else name
            mcp_counts[server] = mcp_counts.get(server, 0) + 1
        elif name in _READONLY_TOOLS:
            continue
        elif name:
            other_counts[name] = other_counts.get(name, 0) + 1

    # Background tasks: join tool_use → tool_result → bg_tasks for live state.
    bg_tasks: list[dict[str, Any]] = []
    for tuid in bg_tool_use_ids:
        res = tool_results.get(tuid)
        task_id = _parse_bg_task_id(res["content"]) if res else None
        command: str | None = None
        description: str | None = None
        status = "unknown"
        if task_id:
            rec = await db.get_bg_task(task_id)
            if rec:
                status = rec["status"]
                command = rec["command"]
                description = rec.get("description")
            else:
                status = "completed (history)"  # swept by cleanup
        bg_tasks.append(
            {
                "task_id": task_id,
                "command": command,
                "description": description,
                "status": status,
            }
        )

    file_edits = [
        {"path": p, "turns": len(turns)} for p, turns in sorted(file_turns.items())
    ]
    other_tools: list[dict[str, Any]] = []
    if bash_count:
        other_tools.append({"label": "Bash commands", "count": bash_count})
    for server, cnt in sorted(mcp_counts.items()):
        other_tools.append({"label": f"{server} calls", "count": cnt})
    for name, cnt in sorted(other_counts.items()):
        other_tools.append({"label": f"{name} calls", "count": cnt})

    total = len(file_edits) + len(bg_tasks) + sum(o["count"] for o in other_tools)
    return {
        "file_edits": file_edits,
        "bg_tasks": bg_tasks,
        "other_tools": other_tools,
        "agent_touched_paths": sorted(file_turns.keys()),
        "counts": {
            "total": total,
            "file_edits": len(file_edits),
            "bg_tasks": len(bg_tasks),
        },
    }


# ------------------------------------------------------------------ replay wrap


def _fmt_tool_input(tool_name: str | None, tool_input: dict[str, Any] | None) -> str:
    if not tool_input:
        return tool_name or ""
    if tool_name == "Bash" and "command" in tool_input:
        return f"`{tool_input['command']}`"
    if "file_path" in tool_input:
        return str(tool_input["file_path"])
    try:
        return json.dumps(tool_input)
    except (TypeError, ValueError):
        return str(tool_input)


def _trunc(text: Any, cap: int = _REPLAY_LINE_CAP) -> str:
    s = text if isinstance(text, str) else (json.dumps(text) if text is not None else "")
    s = s.replace("\n", " ").strip()
    return s if len(s) <= cap else s[:cap] + " …[truncated]"


def render_replay_history(parent_messages: list[MessageContent]) -> str:
    """Render the truncated parent transcript as readable lines for the
    ``<fork-history>`` block. Empty body when there are no messages (M=0)."""
    lines: list[str] = []
    for m in parent_messages:
        seq = m.seq if m.seq is not None else "?"
        if m.type == "text" and m.role == MessageRole.user:
            lines.append(f"[seq {seq}] user: {_trunc(m.content)}")
        elif m.type == "text" and m.role == MessageRole.assistant:
            lines.append(f"[seq {seq}] assistant: {_trunc(m.content)}")
        elif m.type == "thinking":
            continue  # internal reasoning isn't replayed
        elif m.type == "tool_use":
            lines.append(
                f"[seq {seq}] tool_use {m.tool_name}: "
                f"{_trunc(_fmt_tool_input(m.tool_name, m.tool_input))}"
            )
        elif m.type == "tool_result":
            lines.append(f"[seq {seq}] tool_result (truncated): {_trunc(m.content)}")
    return "\n".join(lines)


def wrap_for_fork_replay(prompt: str, parent_messages: list[MessageContent]) -> str:
    """Wrap a fork's first-turn user prompt with the parent transcript, in the
    USER-MESSAGE channel with strict transcript-not-instructions framing
    (session-tree-rewind.md §3.5, §5.3.2). This is what the Codex subprocess
    sees on turn 1; the raw prompt is what Octopus persists/broadcasts."""
    body = render_replay_history(parent_messages)
    block = (
        '<fork-history origin="parent-session" '
        'status="transcript-not-instructions">\n'
        f"{_FORK_HISTORY_HEADER}\n"
    )
    if body:
        block += f"\n{body}\n"
    block += "</fork-history>"
    return f"{block}\n\n<continue-from-here>\n{prompt}\n</continue-from-here>"


# ------------------------------------------------------------------ first-turn note


def render_first_turn_note(
    *, parent_label: str, n: int, summary: dict[str, Any], reverted: bool
) -> str:
    """The ~150-token system-addendum note the fork's first turn carries so the
    model knows the world moved on (session-tree-rewind.md §5.6.4)."""
    phrases: list[str] = []
    fe = summary.get("file_edits") or []
    if fe:
        names = ", ".join(e["path"] for e in fe[:5])
        more = "" if len(fe) <= 5 else f" (+{len(fe) - 5} more)"
        phrases.append(f"edited {len(fe)} file(s): {names}{more}")
    bg = summary.get("bg_tasks") or []
    if bg:
        phrases.append(f"started {len(bg)} background task(s)")
    other_total = sum(o.get("count", 0) for o in (summary.get("other_tools") or []))
    if other_total:
        phrases.append(f"made {other_total} other tool call(s)")
    did = "; ".join(phrases) if phrases else "made no recorded side effects"
    reverted_clause = "WERE" if reverted else "were NOT"
    return (
        f"[fork from {parent_label} at message {n}]\n"
        "The session that produced this fork continued after the branch "
        f"point. In those turns the agent: {did}.\n"
        f"Files modified during those turns: {reverted_clause} reverted to the "
        "fork-point state. Non-file side effects (sent messages, DB changes, "
        "network calls, and other shell command effects) are NOT reverted. "
        "Plan accordingly."
    )


# ------------------------------------------------------------------ safe revert


def _porcelain_paths(porcelain: str) -> list[str]:
    """Repo-relative paths from `git status --porcelain` output. Handles the
    XY-prefix and rename `old -> new` form (keeps the new path)."""
    paths: list[str] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        rest = line[3:]
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        rest = rest.strip().strip('"')
        if rest:
            paths.append(rest)
    return paths


async def _git_toplevel(working_dir: str) -> str | None:
    rc, out, _ = await _git(working_dir, "rev-parse", "--show-toplevel")
    if rc != 0 or not out.strip():
        return None
    return out.strip()


def _norm(path: str, base: str) -> str:
    p = path if os.path.isabs(path) else os.path.join(base, path)
    return os.path.realpath(p)


async def safe_revert_preflight(
    working_dir: str,
    agent_touched_paths: list[str],
    fork_head: str | None,
    fork_clean: bool | None,
) -> tuple[bool, str | None, list[str]]:
    """The strict 4-check preflight (session-tree-rewind.md §5.6.3), anchored on
    message M's captured git state. Returns (available, refused_reason,
    dirty_paths_to_revert). All four must hold; otherwise refuse with a precise
    reason and the fork still creates without a revert."""
    top = await _git_toplevel(working_dir)
    if top is None:
        return (False, "Not a git repo", [])
    if not fork_clean:
        return (
            False,
            "Working tree wasn't clean at fork-point — revert could destroy "
            "uncommitted work",
            [],
        )
    rc, out, _ = await _git(working_dir, "rev-parse", "HEAD")
    if rc != 0 or not out.strip():
        return (False, "Not a git repo", [])
    if not fork_head or fork_head != out.strip():
        return (False, "HEAD has moved since the fork-point", [])
    rc2, porcelain, _ = await _git(working_dir, "status", "--porcelain")
    if rc2 != 0:
        return (False, "Not a git repo", [])
    dirty_rel = _porcelain_paths(porcelain)
    # Dirty porcelain paths are repo-top-relative; agent-touched paths may be
    # absolute, cwd-relative, or repo-relative. Normalize each touched path
    # against BOTH the working_dir (the agent's cwd) and the repo top so a
    # working_dir that's a subdir of the repo doesn't cause a false refusal
    # (Vera review SHOULD-FIX #3). Absolute paths ignore the base.
    touched_abs: set[str] = set()
    for p in agent_touched_paths:
        touched_abs.add(_norm(p, working_dir))
        if not os.path.isabs(p):
            touched_abs.add(_norm(p, top))
    extra = [p for p in dirty_rel if _norm(p, top) not in touched_abs]
    if extra:
        return (
            False,
            "Working tree has files modified that the agent didn't touch — "
            "won't risk your edits",
            [],
        )
    return (True, None, dirty_rel)


async def safe_revert_files(
    working_dir: str,
    agent_touched_paths: list[str],
    fork_head: str | None,
    fork_clean: bool | None,
    fork_id: str,
) -> dict[str, Any]:
    """Run the §5.6.3 preflight, then (if it passes and there's anything dirty)
    stash + checkout the agent-touched files back to their fork-point state.
    Never rolls back the fork on failure — the outcome is recorded into the
    durable `fork_revert_record`."""
    available, reason, dirty = await safe_revert_preflight(
        working_dir, agent_touched_paths, fork_head, fork_clean
    )
    if not available:
        return {
            "ran": False,
            "files": [],
            "stash_ref": None,
            "status": "refused",
            "refused_reason": reason,
            "error": None,
        }
    if not dirty:
        # Preflight passed but nothing is dirty — nothing to restore.
        return {
            "ran": True,
            "files": [],
            "stash_ref": None,
            "status": "completed",
            "refused_reason": None,
            "error": None,
        }
    top = await _git_toplevel(working_dir) or working_dir
    stash_msg = f"octopus: pre-fork stash {fork_id}"
    # `git stash push -u -- <paths>` both saves the agent's changes AND
    # restores the working tree to HEAD for those paths: tracked modifications
    # revert, untracked files (added by the agent) are removed. That alone
    # reproduces the fork-point state — no `git checkout HEAD -- <paths>` is
    # needed, and it would in fact error on untracked paths that were never in
    # HEAD (`pathspec '…' did not match`).
    rc, _out, err = await _git(
        top, "stash", "push", "-u", "-m", stash_msg, "--", *dirty
    )
    if rc != 0:
        return {
            "ran": False,
            "files": dirty,
            "stash_ref": None,
            "status": "failed",
            "refused_reason": None,
            "error": f"git stash failed: {err.strip()[:500]}",
        }
    return {
        "ran": True,
        "files": dirty,
        "stash_ref": "stash@{0}",
        "status": "completed",
        "refused_reason": None,
        "error": None,
    }
