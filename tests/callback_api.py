"""The host an in-turn tool call reaches, for the real-CLI tests.

A tool a real model calls mid-turn is an HTTP request to
`http://127.0.0.1:{settings.port}`, and since the namespaces moved in-process
(polish-2026-09.md §4 B1) there are two halves to it:

* the **REST routes** each tool shims to (`/api/sessions/{sid}/delegations`,
  `…/schedules`, `…/questions`), and
* the **`/mcp/<key>` mounts** the CLI itself connects to over streamable-HTTP.

A test that serves only the first half leaves the model unable to call the tool
at all, which reads exactly like the model declining to call it — the failure
mode `test_delegations_real` was already written to avoid for the REST half.
One helper, so the next transport change has one place to follow rather than a
copy per test file.

Mounting is not enough on its own: each `FastMCP` owns a session manager that
must be running before it will serve, and a mounted sub-app's lifespan is not
run by its parent (see `server/mcp_http.lifespan`).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from typing import Any


@dataclass
class CallbackApi:
    """A live callback host. `port` is what `settings.port` must be pointed at."""

    port: int
    _server: Any
    _task: asyncio.Task
    _stack: contextlib.AsyncExitStack

    async def stop(self) -> None:
        self._server.should_exit = True
        try:
            await asyncio.wait_for(self._task, timeout=10.0)
        except (TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        await self._stack.aclose()


async def start_callback_api(
    *routers: Any,
    mcp: bool = True,
    overrides: dict[Any, Any] | None = None,
) -> CallbackApi:
    """Serve `routers` plus (by default) every MCP namespace on a free port.

    `overrides` goes to `app.dependency_overrides`, which is how a test points
    the routes at its own objects now that they take the session manager as a
    dependency rather than reading a module global (server/deps.py).
    """
    import uvicorn
    from fastapi import FastAPI

    from server.mcp_http import build_servers, mount_all
    from server.mcp_http import lifespan as mcp_lifespan

    app = FastAPI()
    for router in routers:
        app.include_router(router)
    app.dependency_overrides.update(overrides or {})
    # Fresh namespace instances, mounted and started as one set: each test gets
    # its own event loop and its own server, and a FastMCP session manager runs
    # exactly once per instance (see mcp_http.build_servers).
    servers = build_servers(fresh=True) if mcp else {}
    if mcp:
        mount_all(app, servers)

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        # An ephemeral port per test. A fixed one was tried while chasing the
        # cross-test failure recorded in docs/2026-09-25-polish-pass.md and made
        # no difference, so it buys nothing and can only collide.
        port=0,
        # `OCTOPUS_TEST_CALLBACK_LOG=info` turns the access log on, which is how
        # you find out whether a CLI that reported a namespace as unreachable
        # ever actually asked for it.
        log_level=os.environ.get("OCTOPUS_TEST_CALLBACK_LOG", "warning"),
        lifespan="off",
        # No WebSocket routes here, and asking for the implementation imports
        # `websockets.legacy`, whose deprecation warning would then be the
        # suite's only noise.
        ws="none",
    )
    server = uvicorn.Server(config)
    # Signal handlers belong to pytest, not to a server we start mid-test.
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    stack = contextlib.AsyncExitStack()
    if mcp:
        await stack.enter_async_context(mcp_lifespan(app, servers))
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started and server.servers:
            break
        await asyncio.sleep(0.05)
    else:  # pragma: no cover - a stuck uvicorn is a real failure, not a skip
        await stack.aclose()
        raise RuntimeError("callback API server never started")
    return CallbackApi(
        port=server.servers[0].sockets[0].getsockname()[1],
        _server=server,
        _task=task,
        _stack=stack,
    )
