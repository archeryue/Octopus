"""Serving the MCP tool namespaces from the main app instead of spawning them.

Each namespace used to be a subprocess the CLI launched over stdio: seven per
session, ~39 MB PSS each, ~85% of Octopus's own memory and ~247 ms of import
cost apiece on the session-start path (docs/plans/polish-2026-09.md §4 B1;
`mcp_sidecar_count` on the monitor page is now the live version of that
measurement, and reads 0). The tool definitions are unchanged;
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
import contextvars
import functools
import logging
from collections.abc import AsyncIterator
from typing import Any

import anyio.to_thread
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


def _load(module_path: str, *, fresh: bool = False) -> Any:
    """The module's `FastMCP` instance. Importing is safe: every server module
    guards its `mcp.run()` behind `__main__`."""
    import importlib

    module = importlib.import_module(module_path)
    if fresh:
        module = importlib.reload(module)
    return module.mcp


def _threaded(fn: Any) -> Any:
    """An async wrapper that runs `fn` in a worker thread, in this context."""

    @functools.wraps(fn)
    async def run(**kwargs: Any) -> Any:
        # The tool reads its session from a ContextVar the ASGI middleware set
        # (mcp_identity), so the calling context has to come along explicitly —
        # a thread does not inherit one.
        ctx = contextvars.copy_context()
        return await anyio.to_thread.run_sync(functools.partial(ctx.run, fn, **kwargs))

    return run


def _offload_sync_tools(server: Any) -> None:
    """Run `def` tool bodies in a worker thread instead of on the event loop.

    FastMCP calls a sync tool straight on the loop
    (`func_metadata.call_fn_with_arg_validation`). Every tool body in
    `mcp_servers/` is sync and makes a *blocking* HTTP call back into this same
    process, so on the loop it deadlocks: the loop cannot serve the loopback
    request until the tool returns, and the tool cannot return until the request
    is served. The result is one dead 15-second timeout per tool call, plus any
    MCP handshake that a second CLI happened to attempt in that window — which
    is how it presented: "failed to reach Octopus", and `CONNECT_TIMEOUT` on
    another session's namespace discovery.

    As stdio subprocesses the bodies were correct: a blocking call from another
    process cannot starve the server. Moving the transport in-process is what
    made them wrong, so the fix belongs here rather than in nine tool modules —
    and threading a sync handler is what FastAPI does for a `def` endpoint.

    The bound is anyio's default thread limiter (40). `mcp__ask__user` can hold
    its thread for as long as a human takes to answer, so the real ceiling is
    concurrent *blocking* tool calls, not sessions; far above anything one box
    runs, and the alternative — rewriting every body onto an async client — buys
    nothing until that ceiling is in sight.
    """
    tools = getattr(getattr(server, "_tool_manager", None), "_tools", None)
    if not tools:  # pragma: no cover - a namespace with no tools
        return
    for tool in tools.values():
        if tool.is_async:
            continue
        tool.fn = _threaded(tool.fn)
        tool.is_async = True


def build_servers(*, fresh: bool = False) -> dict[str, Any]:
    """The `FastMCP` instance per namespace key.

    `fresh` reloads each module to get *new* instances, which only the tests
    need and which they genuinely cannot do without: a `StreamableHTTPSession-
    Manager` refuses to `run()` twice, the namespace objects are module-level
    singletons, and pytest-asyncio gives each test its own event loop — so a
    server started in one test's loop can neither serve nor be reused in the
    next one's. Production calls this once, at import, and mounts the
    singletons.
    """
    servers = {
        name: _load(path, fresh=fresh)
        for name, path in {**_BUILTIN_MODULES, **_CONNECTOR_MODULES}.items()
    }
    for server in servers.values():
        _offload_sync_tools(server)
    return servers


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
async def lifespan(
    app: FastAPI, servers: dict[str, Any] | None = None
) -> AsyncIterator[None]:
    """Run every mounted namespace's session manager.

    Each `FastMCP` owns a session manager that must be running before it will
    serve, and mounting alone does not start it — the sub-app's own lifespan is
    not run by the parent. An exit stack starts them together and unwinds them
    in reverse on shutdown.

    `servers` must be the very objects that were mounted: starting one set and
    serving another would leave every call hitting a manager that was never
    run. It defaults to the module singletons, which is what production mounts.
    """
    async with contextlib.AsyncExitStack() as stack:
        for name, server in (servers or build_servers()).items():
            await stack.enter_async_context(server.session_manager.run())
            logger.debug("mcp/%s session manager started", name)
        yield


def mount_all(app: FastAPI, servers: dict[str, Any] | None = None) -> list[str]:
    """Mount every namespace. Returns the paths, for logging and for tests."""
    mounted: list[str] = []
    for name, server in (servers or build_servers()).items():
        app.mount(mount_path(name), _ScopeMiddleware(server.streamable_http_app(), name))
        mounted.append(mount_path(name))
    return mounted
