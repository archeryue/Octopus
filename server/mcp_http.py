"""Serving the MCP tool namespaces from the main app instead of spawning them.

Each namespace used to be a subprocess the CLI launched over stdio: seven per
session, ~39 MB PSS each, ~85% of Octopus's own memory and ~247 ms of import
cost apiece on the session-start path (docs/plans/polish-2026-09.md §4 B1,
measured by scripts/measure-footprint.py). The tool definitions are unchanged;
only the transport is. Both CLIs speak streamable-HTTP, and because each
namespace keeps its own mount, the config key — and therefore every
`mcp__<key>__<tool>` name — is preserved exactly.

Connector namespaces mount once per *kind*, not per installation: the config
key stays per-installation (`gmail_e255c1`, so the tool name does not move)
while the URL is shared, and which installation a call belongs to comes from
its verified scope. Key and URL are independent, which is what makes that work.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI

from .mcp_identity import reset_current_scope, set_current_scope, verify

logger = logging.getLogger(__name__)

# Namespace key -> module path. The keys match assembly's builtin set, because
# they are what the tool names are built from.
_BUILTIN_MODULES: dict[str, str] = {
    "bg": "server.mcp_servers.bg",
    "ask": "server.mcp_servers.ask",
    "ask_agent": "server.mcp_servers.ask_agent",
    "research": "server.mcp_servers.research",
    "schedule": "server.mcp_servers.schedule",
}

# Connector kind -> module path. One mount per kind; the installation comes
# from the request scope.
_CONNECTOR_MODULES: dict[str, str] = {
    "github": "server.mcp_servers.connectors.github",
    "gmail": "server.mcp_servers.connectors.gmail",
    "custom": "server.mcp_servers.connectors.custom",
}

# Where a namespace is served. Kept in one place because the harness profiles
# build the same URL when they render a turn's MCP config.
MOUNT_PREFIX = "/mcp"


def mount_path(name: str) -> str:
    return f"{MOUNT_PREFIX}/{name}"


def _load(module_path: str) -> Any:
    """The module's `FastMCP` instance. Importing is safe: every server module
    guards its `mcp.run()` behind `__main__`."""
    import importlib

    return importlib.import_module(module_path).mcp


def _servers() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, path in {**_BUILTIN_MODULES, **_CONNECTOR_MODULES}.items():
        out[name] = _load(path)
    return out


class _ScopeMiddleware:
    """Bind the request's verified scope for the duration of the call.

    Pure ASGI rather than a FastAPI middleware because it wraps a *mounted*
    sub-application, which FastAPI's decorator-based middleware does not reach.
    An unverifiable bearer is left as no scope rather than rejected here, so the
    tool itself can answer the model in words it understands instead of the
    model seeing a bare HTTP error it cannot act on.
    """

    def __init__(self, app: Any, name: str) -> None:
        self.app = app
        self.name = name

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        bearer: str | None = None
        for key, value in scope.get("headers") or []:
            if key == b"authorization":
                raw = value.decode("latin-1")
                if raw.lower().startswith("bearer "):
                    bearer = raw[7:].strip()
                break
        resolved = verify(bearer)
        if resolved is None:
            logger.warning("mcp/%s: call carried no verifiable scope", self.name)
        token = set_current_scope(resolved)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_scope(token)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run every mounted namespace's session manager.

    Each `FastMCP` owns a session manager that must be running before it will
    serve, and mounting alone does not start it — the sub-app's own lifespan is
    not run by the parent. An exit stack starts them together and unwinds them
    in reverse on shutdown.
    """
    async with contextlib.AsyncExitStack() as stack:
        for name, server in _servers().items():
            await stack.enter_async_context(server.session_manager.run())
            logger.debug("mcp/%s session manager started", name)
        yield


def mount_all(app: FastAPI) -> list[str]:
    """Mount every namespace. Returns the paths, for logging and for tests."""
    mounted: list[str] = []
    for name, server in _servers().items():
        app.mount(mount_path(name), _ScopeMiddleware(server.streamable_http_app(), name))
        mounted.append(mount_path(name))
    return mounted
