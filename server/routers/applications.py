"""REST + static-file routes for applications (docs/plans/applications.md §3/§5).

Two routers live here:

  * ``router`` — ``/api/applications``: the CRUD the sidebar and the create
    pane talk to. Bearer auth like every other API route.
  * ``static_router`` — ``/apps/{app_id}/{path}``: the application itself,
    streamed out of its directory. This one can't use plain bearer auth: an
    ``<iframe>`` sends no ``Authorization`` header, and neither do the app's
    own ``<script src>`` / ``fetch`` sub-requests. It accepts the bearer
    header, a ``?token=`` query param, or the ``octopus_app_token`` cookie the
    SPA sets (same-origin, ``path=/apps``) right before mounting the frame.
"""

from __future__ import annotations

import json
import mimetypes
import os
from collections.abc import AsyncIterator
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

from ..app_agent import AppAgentError, app_agent_manager
from ..app_backends import ABSENT, RUNNING, backend_supervisor
from ..applications import (
    ApplicationError,
    ApplicationManager,
    is_app_scope_token,
    resolve_within,
)
from ..auth import verify_token
from ..config import settings
from ..models import (
    AppAgentInfo,
    AppAgentReply,
    AppAgentTurnRequest,
    AppConversation,
    AppConversationDetail,
    ApplicationBackend,
    ApplicationBuildRequest,
    ApplicationCreate,
    ApplicationRead,
    ApplicationUpdate,
)

router = APIRouter(prefix="/api/applications", tags=["applications"])
static_router = APIRouter(tags=["applications"])

# The cookie the SPA sets before mounting an application's iframe. Scoped to
# /apps so it never rides along with API or SPA requests.
APP_TOKEN_COOKIE = "octopus_app_token"

# Injected at startup (mirrors the agents/connectors routers).
_manager: ApplicationManager | None = None


def set_manager(mgr: ApplicationManager) -> None:
    global _manager
    _manager = mgr


def _get_manager() -> ApplicationManager:
    if _manager is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "applications not available"
        )
    return _manager


def _read(row: dict) -> ApplicationRead:
    """One application row as the API shape, with its live backend state.

    The backend isn't a column — it's a running process the supervisor knows
    about — so every response has to merge it in. A helper rather than five
    copies of the same merge, because the one that gets forgotten is the one
    the UI reads.
    """
    return ApplicationRead(
        **row,
        backend=ApplicationBackend(
            **backend_supervisor.describe(row["id"], row["app_dir"])
        ),
    )


def _http_error(e: ApplicationError) -> HTTPException:
    return HTTPException(e.status_code, e.message)


# --------------------------------------------------------------------- API


@router.get("", response_model=list[ApplicationRead])
async def list_applications(
    archived: bool = False, _: str = Depends(verify_token)
):
    """Live applications by default; `?archived=true` returns only the
    archived ones (what the create page's Archived tab lists)."""
    rows = await _get_manager().list_applications(only_archived=archived)
    return [_read(a) for a in rows]


@router.post("", response_model=ApplicationRead, status_code=status.HTTP_201_CREATED)
async def create_application(req: ApplicationCreate, _: str = Depends(verify_token)):
    try:
        app_row = await _get_manager().create_application(**req.model_dump())
    except ApplicationError as e:
        raise _http_error(e)
    return _read(app_row)


@router.get("/{app_id}", response_model=ApplicationRead)
async def get_application(app_id: str, _: str = Depends(verify_token)):
    try:
        return _read(await _get_manager().get_application(app_id))
    except ApplicationError as e:
        raise _http_error(e)


@router.patch("/{app_id}", response_model=ApplicationRead)
async def update_application(
    app_id: str, req: ApplicationUpdate, _: str = Depends(verify_token)
):
    # exclude_unset so omitting a field leaves it untouched while explicitly
    # passing null clears a nullable one (icon).
    try:
        row = await _get_manager().update_application(
            app_id, **req.model_dump(exclude_unset=True)
        )
    except ApplicationError as e:
        raise _http_error(e)
    return _read(row)


@router.post("/{app_id}/build", response_model=ApplicationRead)
async def build_application(
    app_id: str, req: ApplicationBuildRequest, _: str = Depends(verify_token)
):
    """Run another build turn in the application's build session."""
    try:
        row = await _get_manager().request_build(app_id, req.prompt)
    except ApplicationError as e:
        raise _http_error(e)
    return _read(row)


@router.post("/{app_id}/archive", response_model=ApplicationRead)
async def archive_application(app_id: str, _: str = Depends(verify_token)):
    try:
        return ApplicationRead(**await _get_manager().set_archived(app_id, True))
    except ApplicationError as e:
        raise _http_error(e)


@router.post("/{app_id}/unarchive", response_model=ApplicationRead)
async def unarchive_application(app_id: str, _: str = Depends(verify_token)):
    try:
        return ApplicationRead(**await _get_manager().set_archived(app_id, False))
    except ApplicationError as e:
        raise _http_error(e)


@router.delete("/{app_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_application(
    app_id: str, keep_files: bool = False, _: str = Depends(verify_token)
):
    try:
        await _get_manager().delete_application(app_id, keep_files=keep_files)
    except ApplicationError as e:
        raise _http_error(e)


# ------------------------------------------------------------ static serving


def _authorized(request: Request, app_id: str) -> bool:
    """Bearer header, `?token=`, or the app cookie — see the module docstring
    for why the last two exist.

    Plus the application's own **scoped token** (`X-Octopus-App-Token`, or as
    the bearer): the credential a backend script is given so it can reach the
    agent API without ever holding the master token (app-agent-access.md §4).
    It's checked against `app_id`, so app A's token opens nothing of app B's.
    """
    expected = settings.auth_token
    auth = request.headers.get("authorization") or ""
    presented = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if presented and presented == expected:
        return True
    if request.query_params.get("token") == expected:
        return True
    # Percent-decoded, because the client writes the cookie with
    # `encodeURIComponent` — it has to, or a token containing `;` or `,` would
    # truncate the header. Starlette's cookie parser strips quoting but does not
    # decode escapes, so a token with any character JS encodes (`@` in a real
    # one) arrived here as `%40` and failed to match. Every token without such a
    # character is unaffected either way, which is why this survived: it only
    # appears the first time someone rotates to a token that has one.
    if unquote(request.cookies.get(APP_TOKEN_COOKIE) or "") == expected:
        return True
    scoped = request.headers.get("x-octopus-app-token") or presented
    return is_app_scope_token(app_id, scoped)


async def _serve(request: Request, app_id: str, path: str) -> FileResponse:
    if not _authorized(request, app_id):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    try:
        row = await _get_manager().get_application(app_id)
    except ApplicationError as e:
        raise _http_error(e)

    target = resolve_within(row["app_dir"], path or "")
    # A traversal attempt is a 404, not a 403: refusing differently would
    # confirm what exists outside the application directory.
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if os.path.isdir(target):
        target = resolve_within(target, row["entrypoint"])
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if not os.path.isfile(target):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    media_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
    # App content is `no-store`: the whole point is that a rebuild shows up on
    # reload, and a cached index.html would show yesterday's app.
    #
    # The discovered icon is the exception. It's fetched on every render of
    # every sidebar row, so `no-store` means a re-download and a visible
    # flicker each time. It's allowed to be briefly stale instead — and the
    # client appends `?v=<last_built_at>`, so a rebuild busts it immediately.
    cache = "no-store"
    if row.get("icon_src") and path.lstrip("/") == row["icon_src"].lstrip("/"):
        cache = "public, max-age=300"
    return FileResponse(
        target,
        media_type=media_type,
        headers={"Cache-Control": cache},
    )


# Methods a backend may see. Everything a web app needs; nothing that would
# let a request method itself be a surprise.
_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# Hop-by-hop headers belong to one connection and must not be forwarded, plus
# the ones we set ourselves.
_SKIP_REQUEST_HEADERS = {
    "host", "connection", "keep-alive", "transfer-encoding", "upgrade",
    "proxy-authorization", "proxy-connection", "te", "trailer",
    # The app cookie authenticates the request to US. A backend has no use for
    # it, and forwarding it hands the Octopus token to app code.
    "cookie", "authorization",
}
_SKIP_RESPONSE_HEADERS = {
    "connection", "keep-alive", "transfer-encoding", "upgrade", "trailer",
    "content-length", "content-encoding",
}


@static_router.api_route(
    "/apps/{app_id}/api/{path:path}",
    methods=_PROXY_METHODS,
    # Not part of the typed client API: the only caller is an application's own
    # page, calling `api/…` relative to itself. Keeping it out of the schema
    # also avoids FastAPI giving all seven methods of a multi-method route the
    # same operationId, which makes the generated TypeScript uncompilable.
    include_in_schema=False,
)
async def proxy_application_api(request: Request, app_id: str, path: str):
    """`/apps/{id}/api/…` → the application's own backend
    (application-backends.md §7).

    A fixed prefix rather than "serve a static file if one exists, else proxy":
    fallback routing makes whether a request reaches your backend depend on
    whether a file happens to share its path, so renaming a file silently
    changes routing.
    """
    if not _authorized(request, app_id):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    try:
        row = await _get_manager().get_application(app_id)
    except ApplicationError as e:
        raise _http_error(e)

    state = await backend_supervisor.ensure_running(app_id, row["app_dir"])
    if state.state == ABSENT:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "This application has no backend (no executable start.sh)",
        )
    if state.state != RUNNING or not state.port:
        # 503 rather than 500: the app is fine, its backend isn't up. The
        # message carries the reason because a dead backend with no output is
        # the thing this feature exists to avoid.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            state.error or f"backend is {state.state}",
        )

    url = f"http://127.0.0.1:{state.port}/api/{path}"
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _SKIP_REQUEST_HEADERS
    }
    # A long read timeout because a backend may legitimately be slow (a clone,
    # a build); a short connect timeout because a backend we just confirmed is
    # listening should answer the socket immediately.
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0))
    try:
        upstream = client.build_request(
            request.method,
            url,
            headers=headers,
            params=dict(request.query_params),
            content=await request.body(),
        )
        resp = await client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"backend did not answer: {exc}"
        )

    async def body():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        body(),
        status_code=resp.status_code,
        headers={
            k: v for k, v in resp.headers.items()
            if k.lower() not in _SKIP_RESPONSE_HEADERS
        },
        media_type=resp.headers.get("content-type"),
    )


# ------------------------------------------------------------- agent access
#
# An application talking to one of the user's agents (app-agent-access.md §2).
# Mounted under the app's own path so the page reaches it at `agent/…`
# relative to itself — no base URL to configure, no CORS — and reachable from
# the app's backend with the scoped token in `OCTOPUS_APP_TOKEN`.
#
# These MUST stay above the `/apps/{app_id}/{path:path}` catch-all: FastAPI
# matches in registration order, and the catch-all would otherwise swallow
# them. `agent/` and `api/` are therefore reserved prefixes inside an app.


async def _app_for_agent_api(request: Request, app_id: str) -> dict:
    if not _authorized(request, app_id):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    try:
        return await _get_manager().get_application(app_id)
    except ApplicationError as e:
        raise _http_error(e)


def _agent_error(e: AppAgentError) -> HTTPException:
    return HTTPException(e.status_code, e.message)


@static_router.get(
    "/apps/{app_id}/agent/agents", response_model=list[AppAgentInfo]
)
async def list_app_agents(request: Request, app_id: str):
    """Who this app can address. Name is the address."""
    await _app_for_agent_api(request, app_id)
    try:
        return await app_agent_manager.list_agents()
    except AppAgentError as e:
        raise _agent_error(e)


@static_router.get(
    "/apps/{app_id}/agent/conversations", response_model=list[AppConversation]
)
async def list_app_conversations(request: Request, app_id: str):
    await _app_for_agent_api(request, app_id)
    try:
        return app_agent_manager.list_conversations(app_id)
    except AppAgentError as e:
        raise _agent_error(e)


@static_router.get(
    "/apps/{app_id}/agent/conversations/{conversation_id}",
    response_model=AppConversationDetail,
)
async def get_app_conversation(
    request: Request, app_id: str, conversation_id: str
):
    """One thread with its messages — what an app renders after a reload."""
    await _app_for_agent_api(request, app_id)
    try:
        return await app_agent_manager.get_conversation(app_id, conversation_id)
    except AppAgentError as e:
        raise _agent_error(e)


@static_router.delete(
    "/apps/{app_id}/agent/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_app_conversation(
    request: Request, app_id: str, conversation_id: str
):
    await _app_for_agent_api(request, app_id)
    try:
        await app_agent_manager.delete_conversation(app_id, conversation_id)
    except AppAgentError as e:
        raise _agent_error(e)


@static_router.post("/apps/{app_id}/agent/ask", response_model=AppAgentReply)
async def app_agent_ask(
    request: Request, app_id: str, body: AppAgentTurnRequest
):
    """One question, one answer. The shape a backend script wants."""
    row = await _app_for_agent_api(request, app_id)
    try:
        return await app_agent_manager.ask(row, **body.model_dump())
    except AppAgentError as e:
        raise _agent_error(e)


def _sse(events) -> AsyncIterator[bytes]:
    """Frame the manager's event dicts as Server-Sent Events.

    The type goes in the SSE `event:` field *and* inside the JSON, so a client
    can use `addEventListener("delta", …)` or a single `onmessage` handler —
    both are normal, and picking one for the app would be picking wrong half
    the time.
    """

    async def gen() -> AsyncIterator[bytes]:
        async for event in events:
            payload = json.dumps(event, ensure_ascii=False)
            yield f"event: {event['type']}\ndata: {payload}\n\n".encode()

    return gen()


@static_router.post("/apps/{app_id}/agent/chat", include_in_schema=False)
async def app_agent_chat(
    request: Request, app_id: str, body: AppAgentTurnRequest
):
    """The same turn as `ask`, streamed (`text/event-stream`).

    Not in the schema: the response is a stream of events, which an OpenAPI
    response model can't describe, and a lie in the spec is worse than a gap.
    The event vocabulary is documented in app-agent-access.md §2 and in the
    build prompt every application's agent reads.
    """
    row = await _app_for_agent_api(request, app_id)
    try:
        turn = await app_agent_manager.begin_turn(row, **body.model_dump())
    except AppAgentError as e:
        raise _agent_error(e)
    return StreamingResponse(
        _sse(app_agent_manager.stream_turn(turn)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            # Nginx and friends buffer proxied responses by default, which
            # turns a stream into one lump at the end.
            "X-Accel-Buffering": "no",
        },
    )


@static_router.get("/apps/{app_id}")
async def serve_application_root(request: Request, app_id: str):
    """Redirect the bare app URL to a trailing slash so the document's own
    relative URLs (`./app.js`, `style.css`) resolve inside the app instead of
    against `/apps/`."""
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"/apps/{app_id}/{query}")


@static_router.get("/apps/{app_id}/{path:path}")
async def serve_application_file(request: Request, app_id: str, path: str):
    return await _serve(request, app_id, path)
