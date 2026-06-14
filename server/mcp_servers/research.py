"""MCP stdio server: native deep research (native-deep-research.md §7).

Exposes one tool, `mcp__research__deep_research(question)`, a thin HTTP shim to
the `/api/sessions/{sid}/research` route in front of `ResearchManager`. Like
bg/ask_agent, this process is a child of the harness CLI (not FastAPI), so it
calls back over HTTP using the injected env:

  OCTOPUS_API_BASE / OCTOPUS_AUTH_TOKEN / OCTOPUS_SESSION_ID

The job runs in the background; its final cited report is injected into THIS
session as a follow-up turn prefixed `[deep-research:<id>]`. So the tool
returns immediately — like bg/ask_agent, end your turn after calling it.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s research-mcp %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP("octopus-research")


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    if not v:
        logger.error("Required env var %s not set", name)
    return v


@mcp.tool(name="deep_research")
def deep_research(question: str) -> str:
    """Run a deep, multi-source, fact-checked web-research job and get a cited
    report back — asynchronously.

    Octopus fans the work out itself (scope → parallel web searches → claim
    extraction → adversarial verification → synthesis) using THIS backend's
    own web tools, bounded and cancellable. The job runs in the background:
    this tool returns immediately with a research id, and when it finishes the
    cited report is injected into this session as a new turn prefixed
    `[deep-research:<id>]`. After calling, briefly tell the user you've started
    the research, then end your turn — the report arrives as a later turn.

    Use this for questions that need current, multi-source, verified
    information (not for things you already know or can read from the repo).

    Args:
        question: The research question. Make it specific — if it's vague
            (missing scope/constraints), ask the user to narrow it first,
            then pass the refined question here.

    Returns:
        A short string with the research id; the cited report follows later
        as an injected turn.
    """
    api, sid = _env("OCTOPUS_API_BASE"), _env("OCTOPUS_SESSION_ID")
    tok = _env("OCTOPUS_AUTH_TOKEN")
    if not (api and sid and tok):
        return "Error: research server is misconfigured (env vars missing)."
    if not (question or "").strip():
        return "Error: `question` must be a non-empty research question."

    url = f"{api}/api/sessions/{sid}/research"
    try:
        r = httpx.post(
            url,
            json={"question": question},
            headers={"Authorization": f"Bearer {tok}"},
            timeout=15.0,
        )
    except httpx.HTTPError as e:
        return f"Error: failed to reach Octopus to start research: {e}"
    if r.status_code == 409:
        return (
            "Deep research isn't available on this backend (no web tools): "
            f"{r.text[:200]}"
        )
    if r.status_code not in (200, 201):
        return f"Error starting research ({r.status_code}): {r.text[:300]}"
    rid = r.json().get("id", "?")
    return (
        f"Started deep research (id={rid}). It's running in the background; "
        "the cited report will arrive as a later turn prefixed "
        f"[deep-research:{rid}]. Tell the user it's underway and end your turn."
    )


if __name__ == "__main__":
    mcp.run()
