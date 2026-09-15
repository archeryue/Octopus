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

import mimetypes
import os

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse

from ..applications import (
    ApplicationError,
    ApplicationManager,
    resolve_within,
)
from ..auth import verify_token
from ..config import settings
from ..models import (
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
    return [ApplicationRead(**a) for a in rows]


@router.post("", response_model=ApplicationRead, status_code=status.HTTP_201_CREATED)
async def create_application(req: ApplicationCreate, _: str = Depends(verify_token)):
    try:
        app_row = await _get_manager().create_application(**req.model_dump())
    except ApplicationError as e:
        raise _http_error(e)
    return ApplicationRead(**app_row)


@router.get("/{app_id}", response_model=ApplicationRead)
async def get_application(app_id: str, _: str = Depends(verify_token)):
    try:
        return ApplicationRead(**await _get_manager().get_application(app_id))
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
    return ApplicationRead(**row)


@router.post("/{app_id}/build", response_model=ApplicationRead)
async def build_application(
    app_id: str, req: ApplicationBuildRequest, _: str = Depends(verify_token)
):
    """Run another build turn in the application's build session."""
    try:
        row = await _get_manager().request_build(app_id, req.prompt)
    except ApplicationError as e:
        raise _http_error(e)
    return ApplicationRead(**row)


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


def _authorized(request: Request) -> bool:
    """Bearer header, `?token=`, or the app cookie — see the module docstring
    for why the last two exist."""
    expected = settings.auth_token
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer ") and auth[7:].strip() == expected:
        return True
    if request.query_params.get("token") == expected:
        return True
    return request.cookies.get(APP_TOKEN_COOKIE) == expected


async def _serve(request: Request, app_id: str, path: str) -> FileResponse:
    if not _authorized(request):
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
