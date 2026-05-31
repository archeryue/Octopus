"""MCP stdio server exposing one tool: `user` (presented to the model
as `mcp__ask__user`).

Replaces the built-in `AskUserQuestion` tool, which Octopus formerly
intercepted via the CLI control protocol over stdio
(`--permission-prompt-tool=stdio` + a deny-channel hack to inject the
user's answer). That path was load-bearing for AUQ but exposed us to
a CLI premature-exit bug at large context scale (post-mortem in
`docs/post-mortems/2026-05-18-bg-pipeline-hardening.md` §2). The fix is to switch
the CLI to its VM0-style command shape (positional argv prompt, no
`--permission-prompt-tool=stdio`) and rebuild AUQ on top of an MCP
tool that uses regular HTTP to coordinate with the host.

Flow:

  1. Model calls `mcp__ask__user(questions=[...])`.
  2. This MCP server POSTs the questions to
     `/api/sessions/{id}/questions`, gets back a `question_id`.
  3. Octopus broadcasts a `question_request` WS event so the
     frontend renders the form (no shape change vs. the legacy
     path).
  4. This server long-polls
     `/api/sessions/{id}/questions/{question_id}/answer` with a
     60-second per-call window, looping until it gets an answer or
     the session-level auto-answer timeout fires.
  5. When the user (or the auto-answer) submits, the long-poll
     returns the rendered answer text; this server returns that text
     as the tool's result so the model can continue.

The tool's `questions` schema mirrors the built-in AUQ tool: each
question is `{question, header?, multiSelect?, options:[{label,
description}]}`. We don't validate exhaustively — the host re-
validates and the frontend renders whatever it gets.

Spawned by the harness as a child MCP server. Env injected by the harness
assembly (`server/harness/assembly.py:build_callback_env`):
  OCTOPUS_API_BASE     http://127.0.0.1:{settings.port}
  OCTOPUS_AUTH_TOKEN   the bearer token
  OCTOPUS_SESSION_ID   the session this CLI invocation belongs to
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

# Repo root on sys.path so `from server.<x>` works regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s ask-mcp %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


mcp = FastMCP("octopus-ask")


def _env_or_log(name: str) -> str | None:
    v = os.environ.get(name)
    if not v:
        logger.error("Required env var %s not set", name)
    return v


# Outer ceiling for a single ask_user call. The session-level auto-
# answer (default 30 min) handles "user never replies"; this cap is a
# safety net for a hung host. Re-loops the 60s long-polls until met.
_MAX_WAIT_SECONDS = 35 * 60  # 35 min — a bit larger than auto-answer.
_PER_POLL_TIMEOUT = 60.0


@mcp.tool(name="user")
def ask_user(questions: list[dict[str, Any]]) -> str:
    """Ask the user one or more questions and block until they answer.

    Use this whenever you'd previously have called the built-in
    AskUserQuestion tool — that tool is disabled in this environment;
    this is its replacement.

    Each question in the `questions` list is a dict with:
      - question:    str, the question text (required)
      - header:      str, very short label (≤ 12 chars) shown as a
                     chip in the UI (optional)
      - multiSelect: bool, allow multiple answers (default False)
      - options:     list of {label: str, description: str} (2-4
                     options required; the user can also supply "Other"
                     free-text, which the UI provides automatically)

    Returns the user's answer(s) as a single text string, formatted
    by Octopus (something like
    `Q: <question>\\nA: <selected label> (<notes>)`). On session-level
    auto-answer timeout, returns a synthesized "act autonomously"
    instruction the model should follow.
    """
    api = _env_or_log("OCTOPUS_API_BASE")
    sid = _env_or_log("OCTOPUS_SESSION_ID")
    tok = _env_or_log("OCTOPUS_AUTH_TOKEN")
    if not (api and sid and tok):
        return (
            "Error: ask server is misconfigured (env vars missing). "
            "Cannot route this question to the user."
        )
    if not isinstance(questions, list) or not questions:
        return "Error: `questions` must be a non-empty list."

    hdrs = {"Authorization": f"Bearer {tok}"}

    # Step 1: create the pending question on the host.
    try:
        r = httpx.post(
            f"{api}/api/sessions/{sid}/questions",
            json={"questions": questions},
            headers=hdrs,
            timeout=10.0,
        )
    except httpx.HTTPError as e:
        return f"Error: failed to reach Octopus to create question: {e}"
    if r.status_code != 201:
        return f"Error: Octopus rejected the question ({r.status_code}): {r.text[:300]}"
    qid = r.json().get("question_id")
    if not qid:
        return f"Error: Octopus didn't return a question_id: {r.text[:300]}"
    logger.info("created question %s", qid)

    # Step 2: long-poll for the answer, looping per-call timeouts
    # until we get one or hit the outer wait cap.
    deadline = time.monotonic() + _MAX_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            r = httpx.get(
                f"{api}/api/sessions/{sid}/questions/{qid}/answer",
                params={"timeout": _PER_POLL_TIMEOUT},
                headers=hdrs,
                # +5 s padding on httpx-side timeout so we always get
                # the host's 408 rather than tripping our own timeout
                # mid-flight.
                timeout=_PER_POLL_TIMEOUT + 5.0,
            )
        except httpx.HTTPError as e:
            logger.warning("long-poll failed transiently: %s", e)
            continue  # try again
        if r.status_code == 200:
            ans = r.json().get("answer")
            if ans is None:
                return f"Error: host returned 200 with no answer body: {r.text[:300]}"
            logger.info("question %s answered (%d chars)", qid, len(ans))
            return ans
        if r.status_code == 408:
            # Expected — host had no answer within this window. Loop.
            continue
        if r.status_code == 404:
            # Session went away while we were waiting (deleted,
            # archived). Surface so the model knows.
            return (
                f"Error: question {qid} disappeared while waiting "
                f"(session may have been reset or deleted)."
            )
        # Anything else: bail with a clear message.
        return f"Error long-polling answer ({r.status_code}): {r.text[:300]}"

    return (
        f"Error: gave up waiting for an answer after "
        f"{_MAX_WAIT_SECONDS} seconds. The user never replied and "
        "the session-level auto-answer didn't fire either. Proceed "
        "autonomously with the most reasonable choice."
    )


if __name__ == "__main__":
    mcp.run()
