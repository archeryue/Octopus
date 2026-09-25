"""What an in-process MCP tool needs to reach its own host.

The stdio sidecars read three things from their process environment:
`OCTOPUS_API_BASE`, `OCTOPUS_AUTH_TOKEN`, and the session (or connector
installation) they belonged to. Served over HTTP from the main application,
none of those can come from the environment — one process now serves every
session — so they come from settings and from the request's verified scope
instead.

Keeping the tools as HTTP calls to `127.0.0.1` rather than direct manager
calls is deliberate: removing the seven subprocesses is the whole point of
the change, and removing the loopback hop at the same time would rewrite
every tool body for no additional saving.
"""

from __future__ import annotations

from ..config import settings
from ..mcp_identity import McpScope, current_scope


def api_base() -> str:
    return f"http://127.0.0.1:{settings.port}"


def headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.auth_token}",
        "Content-Type": "application/json",
    }


def scope() -> McpScope | None:
    """The verified scope of the call in flight, or None if there isn't one."""
    return current_scope()


def session_id() -> str | None:
    s = current_scope()
    return s.session_id if s else None


def installation_id() -> str | None:
    s = current_scope()
    return s.installation_id if s else None


def resolve(name: str) -> str | None:
    """The value a sidecar used to read from its process environment.

    Kept as a name lookup so every call site in the tool modules stays exactly
    as it was: only where the value comes from changed. The session and
    installation now come from the request's verified scope, and the API base
    and bearer from settings — one process serves every session, so there is no
    per-session environment left to read.
    """
    import os

    # The process environment wins when it is set, and in production it never
    # is: the sidecars that used to be spawned with these variables are gone,
    # and `serve` does not export them. What the fallback buys is a single
    # resolution path that still works outside a request — a test exercising a
    # tool body directly, and `python -m server.mcp_servers.bg` for diagnosing
    # one by hand. Checking it first, rather than last, is what makes those two
    # cases behave identically to the request path instead of subtly differently.
    from_env = os.environ.get(name)
    if from_env:
        return from_env

    if name == "OCTOPUS_API_BASE":
        return api_base()
    if name == "OCTOPUS_AUTH_TOKEN":
        return settings.auth_token
    if name == "OCTOPUS_SESSION_ID":
        return session_id()
    if name == "OCTOPUS_INSTALLATION_ID":
        return installation_id()
    return None


NO_SCOPE = (
    "Error: this tool call carried no recognised session. "
    "Octopus could not tell which conversation it belongs to."
)
