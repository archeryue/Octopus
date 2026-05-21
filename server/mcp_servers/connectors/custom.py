"""Generic MCP server for custom (user-defined) connectors.

One module serves every custom connector kind: it reads the connector's API
base from env (injected per-installation by CustomConnector.mcp_entry) and
exposes a single authenticated `request` tool the agent uses to call that API.
Token fetch + reconnect handling are shared with the typed connectors.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

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
    format="%(asctime)s custom-mcp %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP("octopus-custom-connector")
ctx = ConnectorContext()

_RECONNECT = (
    "Error: token expired or revoked — ask the user to reconnect this "
    "connector in Octopus's sidebar."
)


def _api_base() -> str:
    return (os.environ.get("OCTOPUS_CONNECTOR_API_BASE") or "").rstrip("/")


@mcp.tool(name="request")
def request(
    method: str, path: str, query: dict | None = None, body: Any = None
) -> str:
    """Make an authenticated HTTP call to this connector's API. `method` is
    GET/POST/PATCH/DELETE/…; `path` is relative to the connector's API base
    (e.g. '/issues'); `query` are optional query params; `body` is an optional
    JSON body for writes. The connector's OAuth token is attached automatically.
    """
    token = ctx.access_token()
    if token is None:
        return "Error: connector unavailable — reconnect it in Octopus."
    api_base = _api_base()
    if not api_base:
        return "Error: connector misconfigured (no API base)."
    url = path if path.startswith("http") else f"{api_base}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    for attempt in range(3):
        try:
            r = httpx.request(
                method.upper(),
                url,
                headers=headers,
                params=query,
                json=body,
                timeout=30.0,
            )
        except httpx.HTTPError as e:
            return f"Error: request failed: {e}"
        if r.status_code == 401:
            ctx.mark_needs_reconnect("invalid_grant")
            return _RECONNECT
        if r.status_code == 429 or r.status_code >= 500:
            if attempt < 2:
                time.sleep(min(float(r.headers.get("Retry-After", "1") or 1), 5.0))
                continue
            return f"Error: upstream unavailable ({r.status_code})."
        if r.status_code >= 400:
            return f"Error: HTTP {r.status_code}: {r.text[:300]}"
        try:
            return truncate(to_text(r.json()))
        except Exception:
            return truncate(r.text)
    return "Error: rate-limited; try again shortly."


if __name__ == "__main__":
    mcp.run()
