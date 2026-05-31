"""MCP stdio server: agent-to-agent delegation (agent-collaboration.md).

The `ask_agent` server exposes four tools to the model. Their full
MCP names are `mcp__ask_agent__<tool>` (the prefix is the config key
under which the harness mounts this server, not the FastMCP server
name); inside this module the underlying Python functions are
``ask_agent`` / ``cancel_agent_task`` / ``answer_agent_question`` /
``list_agent_tasks`` and the ``@mcp.tool(name=…)`` decorators expose
the short forms ``ask`` / ``cancel`` / ``answer`` / ``list``.

  - `ask(request, name=…, delegation_id=…, files=…)` — bimodal
    delegation. Pass `name` to start a fresh delegation under a
    target agent; pass `delegation_id` to continue a prior
    delegation in the SAME child session (so the other agent keeps
    her in-session transcript across rounds — review iterations,
    "now apply that same review to file Y"). Exactly one of the two
    ids must be set. Returns a `delegation_id` immediately and ends
    the current turn; when the other agent finishes, Octopus
    auto-fires a follow-up turn into this session prefixed
    `[agent-reply:<name> delegation=<id>]` carrying the other
    agent's reply. The two modes were originally separate `ask` and
    `follow_up` tools; merged into one to keep the model's tool
    surface small (fewer per-turn-context tokens, one decision
    point instead of two).

  - `cancel` — best-effort stop a running delegation. Idempotent.

  - `answer` — answer a question a delegated agent raised via its
    own `ask` MCP tool. The other agent's pending question is
    drained with the parent's chosen label and the other agent
    resumes (agent-collaboration.md §5.5).

  - `list` — recent delegations from this session (most-recent
    first). Useful on a resumed turn to disambiguate multiple
    concurrent delegations by id.

Channel: this process is a child of the harness CLI (claude / codex),
NOT of Octopus's FastAPI server. We can't reach the DelegationManager
singleton directly — we have to go over HTTP. The parent Octopus
process injects three env vars when spawning us:

  OCTOPUS_API_BASE     e.g. "http://127.0.0.1:8000"
  OCTOPUS_AUTH_TOKEN   the same bearer token everything else uses
  OCTOPUS_SESSION_ID   the parent session this CLI invocation is bound
                       to (the delegation will be hung off this id)

The session id scopes "this delegation belongs to this chat" — we
don't trust the model to pass it correctly, so it's not a tool
parameter.

Shape mirrors `server/mcp_servers/bg.py` deliberately; the bg pattern
is the right shape for any cross-turn fire-and-forget operation whose
result is delivered as an injected follow-up turn.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Keep the import-path mirror from bg.py — the harness spawns us with
# arbitrary cwd, so import-style "server.foo" needs the repo root on
# sys.path even when PYTHONPATH wasn't set.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s ask_agent-mcp %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


mcp = FastMCP("octopus-ask-agent")


def _required_env(name: str) -> str | None:
    v = os.environ.get(name)
    if not v:
        logger.error("Required env var %s not set", name)
    return v


def _api_base() -> str | None:
    return _required_env("OCTOPUS_API_BASE")


def _session_id() -> str | None:
    return _required_env("OCTOPUS_SESSION_ID")


def _headers() -> dict[str, str] | None:
    tok = _required_env("OCTOPUS_AUTH_TOKEN")
    if not tok:
        return None
    return {"Authorization": f"Bearer {tok}"}


@mcp.tool(name="ask")
def ask_agent(
    request: str,
    name: str | None = None,
    delegation_id: str | None = None,
    files: list[str] | None = None,
) -> str:
    """Delegate work to another agent and wait — across turns — for
    their reply. Returns a delegation id immediately; the other
    agent's reply arrives as a follow-up turn into THIS session,
    prefixed `[agent-reply:<name> delegation=<id>]`.

    Two modes, picked by which id you pass:

    1) **Fresh delegation** — pass `name`. The other agent runs in a
       new child session with no context from this conversation; brief
       them in `request` as if they just walked into the room.

    2) **Continuation of a prior round** — pass `delegation_id` from a
       previous reply you got from the same agent. The other agent's
       earlier turn is still in her transcript, so she resumes with
       full context — ideal for review rounds, "now apply that same
       review to file Y", or any iteration on the same line of work.
       Don't repeat the original brief; just say what's new.

    Use mode (1) for unrelated work or **parallel fan-out** (multiple
    in-flight delegations to one target need separate sessions to run
    concurrently). Use mode (2) to save the other agent re-reading
    everything when you're continuing the same line of work.

    Exactly ONE of (`name`, `delegation_id`) must be set.

    Use this when the other agent is better placed to do the work
    (different skills, different tool access, a fresh context). Do NOT
    use it to offload work you can do yourself in this turn — the
    cross-turn round-trip costs latency and tokens.

    After calling, briefly tell the user what you asked and end your
    turn. When the follow-up arrives, relay or build on the reply.

    Args:
        request: What you want the other agent to do. For mode 1
            (fresh), write it self-contained: name files, paste
            snippets, spell out goals. For mode 2 (continuation), say
            only what's NEW — the original brief and the previous
            reply are already in the other agent's transcript.
        name: The other agent's display name (e.g. "Vera",
            "Researcher"). Required for mode 1. Case-insensitive;
            ambiguous matches are rejected.
        delegation_id: The id from an earlier reply (`[agent-reply:…
            delegation=<id>]`) to continue. Required for mode 2. The
            delegation must be in a terminal state — wait for the
            reply before continuing.
        files: Optional list of paths the other agent should read.
            Mode 1 only; ignored for continuations (the prior turn's
            file references are already in the transcript).

    Returns:
        A short string with the delegation id. Cite that id back to
        the user if they ask "where's that delegation?".
    """
    api = _api_base()
    sid = _session_id()
    hdrs = _headers()
    if not (api and sid and hdrs):
        return (
            "Error: ask_agent server is misconfigured (env vars "
            "missing); cannot start a delegation."
        )
    if not (request or "").strip():
        return "Error: `request` must be a non-empty description of the ask."

    name = (name or "").strip() or None
    delegation_id = (delegation_id or "").strip() or None
    if (name is None) == (delegation_id is None):
        return (
            "Error: exactly one of `name` (fresh delegation) or "
            "`delegation_id` (continue prior) must be provided."
        )

    if delegation_id is not None:
        # Continuation mode — same child session, model resumes with
        # prior transcript in view.
        url = (
            f"{api}/api/sessions/{sid}/delegations/"
            f"{delegation_id}/follow-up"
        )
        body: dict[str, object] = {"request": request}
        try:
            r = httpx.post(url, json=body, headers=hdrs, timeout=10.0)
        except httpx.HTTPError as e:
            return (
                f"Error: failed to reach Octopus to continue "
                f"delegation {delegation_id}: {e}"
            )
        if r.status_code == 404:
            return (
                f"No delegation `{delegation_id}` for this session — "
                f"maybe it's from a different parent. Pass `name` "
                f"instead to start fresh."
            )
        if r.status_code == 409:
            return (
                f"Cannot continue `{delegation_id}` right now: "
                f"{r.text[:300]}. Pass `name` instead to start a "
                f"fresh delegation."
            )
        if r.status_code not in (200, 201):
            return (
                f"Error continuing delegation `{delegation_id}` "
                f"({r.status_code}): {r.text[:300]}"
            )
        data = r.json()
        did = data.get("delegation_id", delegation_id)
        target = data.get("target_agent_name") or "the other agent"
        return (
            f"Continued delegation `{did}` with {target} in the same "
            f"session — they have the previous round in their "
            f"transcript. Tell the user briefly what you asked, then "
            f"end your turn. The reply will arrive as a follow-up "
            f"turn prefixed `[agent-reply:{target} delegation={did}]`."
        )

    # Fresh-delegation mode.
    url = f"{api}/api/sessions/{sid}/delegations"
    body = {
        "agent_name": name,
        "request": request,
    }
    if files:
        body["files"] = files
    try:
        r = httpx.post(url, json=body, headers=hdrs, timeout=10.0)
    except httpx.HTTPError as e:
        return f"Error: failed to reach Octopus to start the delegation: {e}"
    if r.status_code == 404:
        return f"No agent named {name!r}, and no parent session match."
    if r.status_code == 409:
        return f"Cannot delegate: {r.text[:300]}"
    if r.status_code != 201:
        return (
            f"Error: Octopus rejected the delegation "
            f"({r.status_code}): {r.text[:300]}"
        )
    data = r.json()
    did = data.get("delegation_id", "?")
    target = data.get("target_agent_name") or name
    return (
        f"Started delegation `{did}` to {target}. They are working "
        f"on it in the background. Tell the user briefly what you "
        f"asked, then end your turn — don't wait. A follow-up turn "
        f"prefixed `[agent-reply:{target} delegation={did}]` (or "
        f"`[agent-error:…]`) will arrive when they're done."
    )


@mcp.tool(name="cancel")
def cancel_agent_task(delegation_id: str, reason: str | None = None) -> str:
    """Cancel a running delegation. Idempotent — cancelling a
    finished delegation is a no-op that returns ok-ish text. The
    other agent's in-flight turn is interrupted; an
    `[agent-error:…reason=cancelled…]` follow-up turn is injected
    into this session so you (and the user) see the terminal state.

    Args:
        delegation_id: The id returned by an earlier `ask_agent` call.
        reason: Optional short reason for the cancel (shown in the
            error injection so the user / next turn understands why).

    Returns:
        A short text summary of the cancel outcome.
    """
    api = _api_base()
    sid = _session_id()
    hdrs = _headers()
    if not (api and sid and hdrs):
        return "Error: ask_agent server is misconfigured (env vars missing)."
    url = (
        f"{api}/api/sessions/{sid}/delegations/{delegation_id}/cancel"
    )
    body = {"reason": reason} if reason else {}
    try:
        r = httpx.post(url, json=body, headers=hdrs, timeout=10.0)
    except httpx.HTTPError as e:
        return f"Error: failed to reach Octopus to cancel {delegation_id}: {e}"
    if r.status_code == 404:
        return f"No delegation `{delegation_id}` for this session."
    if r.status_code != 200:
        return (
            f"Error cancelling delegation `{delegation_id}` "
            f"({r.status_code}): {r.text[:300]}"
        )
    data = r.json()
    state = data.get("state", "?")
    return (
        f"Delegation `{delegation_id}` is in state {state}. A "
        f"terminal follow-up turn will arrive shortly if it wasn't "
        f"already finished."
    )


@mcp.tool(name="answer")
def answer_agent_question(delegation_id: str, choice: str) -> str:
    """Answer a question a delegated agent raised. Use this when a
    follow-up turn arrives with the prefix
    `[agent-question:<name> delegation=<id> …]` AND you can answer it
    yourself from this session's context.

    If you DON'T know the answer, do NOT guess. Instead, use the
    `mcp__ask__user` tool to ask the user in THIS session for the
    information you need, then forward their answer here. This is
    the principal-chain rule from the agent-collaboration design:
    questions travel one hop up the chain, the parent decides
    whether to answer or escalate.

    Args:
        delegation_id: The id from the `[agent-question:…]` prefix.
            Same id `ask_agent` returned originally.
        choice: The label you picked from the option list. For a
            single-choice question, one label. For multi-select, the
            label you most want — only the first selection is sent
            in v1 (multi-select forwarding is a future polish).

    Returns:
        A short text confirmation. The other agent's turn resumes
        and will eventually drop a `[agent-reply:…]` turn back here.
    """
    api = _api_base()
    sid = _session_id()
    hdrs = _headers()
    if not (api and sid and hdrs):
        return "Error: ask_agent server is misconfigured (env vars missing)."
    if not (delegation_id or "").strip():
        return "Error: `delegation_id` must be a non-empty string."
    if not (choice or "").strip():
        return "Error: `choice` must be a non-empty string."

    url = (
        f"{api}/api/sessions/{sid}/delegations/"
        f"{delegation_id}/answer"
    )
    body = {"choice": choice}
    try:
        r = httpx.post(url, json=body, headers=hdrs, timeout=10.0)
    except httpx.HTTPError as e:
        return f"Error: failed to reach Octopus to answer {delegation_id}: {e}"
    if r.status_code == 404:
        return (
            f"No delegation `{delegation_id}` for this session "
            f"(maybe it already finished, or the id was wrong)."
        )
    if r.status_code == 409:
        return f"Cannot answer: {r.text[:300]}"
    if r.status_code != 200:
        return (
            f"Error answering delegation `{delegation_id}` "
            f"({r.status_code}): {r.text[:300]}"
        )
    return (
        f"Answered delegation `{delegation_id}` with `{choice}`. The "
        f"other agent will resume; their reply will arrive as a "
        f"follow-up turn."
    )


@mcp.tool(name="list")
def list_agent_tasks() -> str:
    """List recent delegations from this session, most-recent first.

    Use this when a follow-up turn references a delegation id the
    chat history is too long to scroll for, or when the user asks
    "what agents are working for you right now?". The list is capped
    server-side (25 entries).

    Returns:
        A short text summary. For programmatic detail, the user can
        check the UI delegation card popover.
    """
    api = _api_base()
    sid = _session_id()
    hdrs = _headers()
    if not (api and sid and hdrs):
        return "Error: ask_agent server is misconfigured (env vars missing)."
    url = f"{api}/api/sessions/{sid}/delegations"
    try:
        r = httpx.get(url, headers=hdrs, timeout=10.0)
    except httpx.HTTPError as e:
        return f"Error: failed to reach Octopus to list delegations: {e}"
    if r.status_code != 200:
        return f"Error listing delegations ({r.status_code}): {r.text[:300]}"
    items = r.json()
    if not items:
        return "No delegations from this session yet."
    lines = [f"{len(items)} delegation(s) — most recent first:"]
    for item in items[:25]:
        line = (
            f"  • {item['delegation_id']}  → "
            f"{item.get('target_agent_name', '?')}  "
            f"[{item.get('state', '?')}]"
        )
        if item.get("error"):
            line += f"  (error={item['error'][:60]})"
        lines.append(line)
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
