"""GitHub connector MCP server (connectors.md Phase B / §6).

Spawned as `python -m server.mcp_servers.connectors.github` with the shared
callback env + OCTOPUS_INSTALLATION_ID. Each tool fetches the installation's
token from the host, calls the GitHub REST API, projects the response to the
fields the model needs, and caps the result size. 401 → mark the installation
needs-reconnect and tell the model to ask the user; 429/5xx → bounded retry.
"""

from __future__ import annotations

import base64
import logging
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# Make `server` importable regardless of the spawning cwd (mirrors viewer.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from server.mcp_servers.connectors._shared import (  # noqa: E402
    ConnectorContext,
    to_text,
    truncate,
)

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s github-mcp %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP("octopus-github")
ctx = ConnectorContext()

_API = "https://api.github.com"
_GH_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
_RECONNECT_MSG = (
    "Error: GitHub token expired or revoked — ask the user to reconnect "
    "GitHub in Octopus's sidebar."
)


def _gh(method: str, path: str, **kw: Any) -> tuple[Any | None, str | None]:
    """Authenticated GitHub call. Returns (parsed_body, None) on success or
    (None, error_message) — the error string is itself a fine tool result."""
    token = ctx.access_token()
    if token is None:
        return None, "Error: connector unavailable — reconnect GitHub in Octopus."
    headers = {**_GH_HEADERS, "Authorization": f"Bearer {token}"}
    url = path if path.startswith("http") else f"{_API}{path}"
    for attempt in range(3):
        try:
            r = httpx.request(method, url, headers=headers, timeout=30.0, **kw)
        except httpx.HTTPError as e:
            return None, f"Error: GitHub request failed: {e}"
        if r.status_code == 401:
            ctx.mark_needs_reconnect("invalid_grant")
            return None, _RECONNECT_MSG
        if r.status_code == 429 or r.status_code >= 500:
            if attempt < 2:
                retry_after = float(r.headers.get("Retry-After", "1") or 1)
                time.sleep(min(retry_after, 5.0))
                continue
            return None, f"Error: GitHub temporarily unavailable ({r.status_code})."
        if r.status_code >= 400:
            return None, f"Error: GitHub {r.status_code}: {r.text[:300]}"
        return (r.json() if r.content else {}), None
    return None, "Error: GitHub rate-limited; try again shortly."


def _issue_brief(it: dict) -> dict:
    repo = ""
    if it.get("repository_url"):
        repo = it["repository_url"].split("/repos/", 1)[-1]
    return {
        "repo": repo,
        "number": it.get("number"),
        "title": it.get("title"),
        "state": it.get("state"),
        "is_pr": "pull_request" in it,
        "url": it.get("html_url"),
    }


@mcp.tool(name="search_issues")
def search_issues(query: str, limit: int = 20) -> str:
    """Search issues and pull requests with GitHub search syntax
    (e.g. 'repo:owner/name is:open label:bug'). Returns brief matches."""
    data, err = _gh(
        "GET", "/search/issues", params={"q": query, "per_page": min(limit, 50)}
    )
    if err:
        return err
    items = [_issue_brief(it) for it in data.get("items", [])]
    return truncate(to_text({"total": data.get("total_count"), "items": items}))


@mcp.tool(name="get_issue")
def get_issue(repo: str, number: int) -> str:
    """Full issue: title, state, author, labels, and body. `repo` is
    'owner/name'."""
    data, err = _gh("GET", f"/repos/{repo}/issues/{number}")
    if err:
        return err
    out = {
        "number": data.get("number"),
        "title": data.get("title"),
        "state": data.get("state"),
        "author": (data.get("user") or {}).get("login"),
        "labels": [lbl.get("name") for lbl in data.get("labels", [])],
        "comments": data.get("comments"),
        "url": data.get("html_url"),
        "body": data.get("body") or "",
    }
    return truncate(to_text(out))


@mcp.tool(name="get_pr")
def get_pr(repo: str, number: int) -> str:
    """Full pull request: title, state, head→base branches, merge state,
    and body. `repo` is 'owner/name'."""
    data, err = _gh("GET", f"/repos/{repo}/pulls/{number}")
    if err:
        return err
    out = {
        "number": data.get("number"),
        "title": data.get("title"),
        "state": data.get("state"),
        "merged": data.get("merged"),
        "draft": data.get("draft"),
        "head": (data.get("head") or {}).get("ref"),
        "base": (data.get("base") or {}).get("ref"),
        "author": (data.get("user") or {}).get("login"),
        "url": data.get("html_url"),
        "body": data.get("body") or "",
    }
    return truncate(to_text(out))


@mcp.tool(name="list_repos")
def list_repos(limit: int = 30) -> str:
    """The authenticated user's repositories, most-recently-updated first."""
    data, err = _gh(
        "GET",
        "/user/repos",
        params={"per_page": min(limit, 100), "sort": "updated"},
    )
    if err:
        return err
    repos = [
        {
            "full_name": r.get("full_name"),
            "private": r.get("private"),
            "description": r.get("description"),
            "url": r.get("html_url"),
        }
        for r in data
    ]
    return truncate(to_text(repos))


@mcp.tool(name="get_file")
def get_file(repo: str, path: str, ref: str | None = None) -> str:
    """Read a file's text from a repo. `repo` is 'owner/name'; `ref` is an
    optional branch/tag/sha."""
    params = {"ref": ref} if ref else None
    data, err = _gh("GET", f"/repos/{repo}/contents/{path}", params=params)
    if err:
        return err
    if isinstance(data, list):  # a directory
        return truncate(to_text([{"name": e.get("name"), "type": e.get("type")} for e in data]))
    if data.get("encoding") == "base64" and data.get("content") is not None:
        try:
            text = base64.b64decode(data["content"]).decode("utf-8", "replace")
        except Exception as e:  # binary or malformed
            return f"Error: could not decode {path}: {e}"
        return truncate(text)
    return truncate(to_text(data))


@mcp.tool(name="search_code")
def search_code(query: str, limit: int = 20) -> str:
    """Search code with GitHub code-search syntax (e.g. 'addEventListener
    repo:owner/name'). Returns matching file paths."""
    data, err = _gh(
        "GET", "/search/code", params={"q": query, "per_page": min(limit, 50)}
    )
    if err:
        return err
    hits = [
        {
            "repo": (it.get("repository") or {}).get("full_name"),
            "path": it.get("path"),
            "url": it.get("html_url"),
        }
        for it in data.get("items", [])
    ]
    return truncate(to_text({"total": data.get("total_count"), "items": hits}))


@mcp.tool(name="create_issue")
def create_issue(repo: str, title: str, body: str = "") -> str:
    """Open a new issue. This WRITES to GitHub — explain what you're filing
    before calling. `repo` is 'owner/name'."""
    data, err = _gh(
        "POST", f"/repos/{repo}/issues", json={"title": title, "body": body}
    )
    if err:
        return err
    return to_text(
        {"created": True, "number": data.get("number"), "url": data.get("html_url")}
    )


@mcp.tool(name="comment")
def comment(repo: str, number: int, body: str) -> str:
    """Comment on an issue or PR. This WRITES to GitHub. `repo` is
    'owner/name'; `number` is the issue/PR number."""
    data, err = _gh(
        "POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body}
    )
    if err:
        return err
    return to_text({"created": True, "url": data.get("html_url")})


if __name__ == "__main__":
    mcp.run()
