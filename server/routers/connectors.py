"""REST endpoints for connectors (connectors.md §5.5).

Installations are global (one OAuth-authorized account each); enablement is
AGENT-scoped via the agent_connectors join (revision 2026-05-20). Secrets are
never returned over the wire — the only consumer of a token is the connector
MCP subprocess, which fetches it from the internal `/token` route at call time.

The OAuth callback is the one UNauthenticated route here: the third party
redirects the user's browser to it, so it can't carry our bearer; the `state`
parameter (login_id + random half) is the trust anchor instead.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse

from ..auth import verify_token
from ..connector_manager import ConnectorError, ConnectorManager
from ..connectors.oauth import ConnectorLoginError, ConnectorLoginManager
from ..models import (
    AgentConnectorsResponse,
    ConnectorCatalogEntry,
    ConnectorInstallationInfo,
    ConnectorOAuthCancelRequest,
    ConnectorOAuthClientInfo,
    ConnectorOAuthStartRequest,
    ConnectorOAuthStartResponse,
    ConnectorOAuthStatusResponse,
    ConnectorTokenResponse,
    CustomConnectorCreateRequest,
    SetAgentConnectorsRequest,
    SetConnectorOAuthClientRequest,
    ToggleAgentConnectorRequest,
    UpdateConnectorRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/connectors", tags=["connectors"])
# Agent-scoped enable routes share the /api/agents space (FastAPI merges
# routers on the same prefix; the deeper /connectors paths don't collide with
# the agents router's /{agent_id}).
agent_router = APIRouter(prefix="/api/agents", tags=["connectors"])

_manager: ConnectorManager | None = None
# In-memory OAuth state — owned here, like credentials' oauth_login_manager.
_login_mgr = ConnectorLoginManager()


def set_manager(mgr: ConnectorManager) -> None:
    global _manager
    _manager = mgr


def _require_manager() -> ConnectorManager:
    if _manager is None:
        raise HTTPException(status_code=503, detail="connectors not initialized")
    return _manager


def _http_error(e: ConnectorError) -> HTTPException:
    msg = str(e).lower()
    code = 404 if ("not found" in msg or "unknown" in msg) else 400
    return HTTPException(status_code=code, detail=str(e))


def _to_info(row: dict) -> ConnectorInstallationInfo:
    return ConnectorInstallationInfo(
        id=row["id"],
        kind=row["kind"],
        label=row["label"],
        auth_type=row.get("auth_type") or "oauth",
        external_account_id=row.get("external_account_id"),
        scopes=row.get("scopes") or [],
        enable_by_default=bool(row.get("enable_by_default", False)),
        needs_reconnect=bool(row.get("needs_reconnect", False)),
        token_expires_at=row.get("token_expires_at"),
        last_refresh_error_code=row.get("last_refresh_error_code"),
        created_at=row["created_at"],
    )


def _public_base(request: Request) -> str:
    """The browser-facing base URL, derived from the request so a remote /
    tunneled user (no server env access) gets a redirect URI that matches what
    they registered with the provider. Honors proxy forwarding headers."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    return f"{proto}://{host}" if host else str(request.base_url).rstrip("/")


def _callback_page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8">
<title>{title}</title></head>
<body style="font-family:system-ui;padding:2rem;text-align:center">
<h2>{title}</h2><p>{body}</p>
<script>setTimeout(function(){{window.close()}},800)</script>
</body></html>"""
    )


# --- catalog + installations ----------------------------------------------


@router.get("/catalog", response_model=list[ConnectorCatalogEntry])
async def list_catalog(_: str = Depends(verify_token)):
    return [ConnectorCatalogEntry(**e) for e in await _require_manager().catalog()]


@router.get("/{kind}/oauth-client", response_model=ConnectorOAuthClientInfo)
async def get_oauth_client(
    kind: str, request: Request, _: str = Depends(verify_token)
):
    try:
        return ConnectorOAuthClientInfo(
            **await _require_manager().client_config(kind, _public_base(request))
        )
    except ConnectorError as e:
        raise _http_error(e)


@router.put("/{kind}/oauth-client", response_model=ConnectorOAuthClientInfo)
async def set_oauth_client(
    kind: str,
    req: SetConnectorOAuthClientRequest,
    request: Request,
    _: str = Depends(verify_token),
):
    mgr = _require_manager()
    try:
        await mgr.set_client_creds(kind, req.client_id, req.client_secret)
        return ConnectorOAuthClientInfo(
            **await mgr.client_config(kind, _public_base(request))
        )
    except ConnectorError as e:
        raise _http_error(e)


@router.delete("/{kind}/oauth-client", status_code=status.HTTP_204_NO_CONTENT)
async def delete_oauth_client(kind: str, _: str = Depends(verify_token)):
    await _require_manager().clear_client_creds(kind)


# --- custom (user-defined) connectors -------------------------------------


@router.post(
    "/custom",
    response_model=ConnectorCatalogEntry,
    status_code=status.HTTP_201_CREATED,
)
async def create_custom_connector(
    req: CustomConnectorCreateRequest, _: str = Depends(verify_token)
):
    mgr = _require_manager()
    try:
        await mgr.create_custom_connector(**req.model_dump())
    except ConnectorError as e:
        raise _http_error(e)
    # Return the new catalog entry (available, since creds were just set).
    entry = next(c for c in await mgr.catalog() if c["kind"] == req.kind.strip().lower())
    return ConnectorCatalogEntry(**entry)


@router.delete("/custom/{kind}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_custom_connector(kind: str, _: str = Depends(verify_token)):
    try:
        await _require_manager().delete_custom_connector(kind)
    except ConnectorError as e:
        raise _http_error(e)


@router.get("", response_model=list[ConnectorInstallationInfo])
async def list_installations(_: str = Depends(verify_token)):
    rows = await _require_manager().list_installations()
    return [_to_info(r) for r in rows]


# --- OAuth install flow ----------------------------------------------------


@router.post(
    "/oauth/start",
    response_model=ConnectorOAuthStartResponse,
    status_code=status.HTTP_201_CREATED,
)
async def oauth_start(
    req: ConnectorOAuthStartRequest, request: Request, _: str = Depends(verify_token)
):
    mgr = _require_manager()
    connector = await mgr.get(req.kind)
    if connector is None:
        raise HTTPException(status_code=404, detail=f"unknown connector: {req.kind}")
    creds = await mgr.resolve_client_creds(req.kind)
    if creds is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{req.kind} is not configured — add its OAuth client id and "
                "secret first (Set up)"
            ),
        )
    redirect_uri = f"{_public_base(request)}/api/connectors/oauth/callback"
    pl = _login_mgr.start(
        provider=connector.oauth,
        client_id=creds[0],
        redirect_uri=redirect_uri,
        requested_label=req.label,
    )
    return ConnectorOAuthStartResponse(login_id=pl.login_id, authorize_url=pl.authorize_url)


@router.get("/oauth/callback")
async def oauth_callback(
    code: str | None = Query(default=None),
    state: str = Query(default=""),
    error: str | None = Query(default=None),
):
    """Third-party browser redirect lands here. No bearer — `state` is the
    CSRF anchor. Returns a small self-closing HTML page either way."""
    try:
        pl = _login_mgr.resolve_callback(state)
    except ConnectorLoginError as e:
        return _callback_page("Connection failed", str(e))

    if error:
        _login_mgr.mark_error(pl.login_id, error)
        return _callback_page("Connection cancelled", f"Provider returned: {error}")
    if not code:
        _login_mgr.mark_error(pl.login_id, "no authorization code")
        return _callback_page("Connection failed", "No authorization code returned.")

    connector = await _require_manager().get(pl.kind)
    if connector is None:
        _login_mgr.mark_error(pl.login_id, "connector kind disappeared")
        return _callback_page("Connection failed", "Connector no longer registered.")
    creds = await _require_manager().resolve_client_creds(pl.kind)
    if creds is None:
        _login_mgr.mark_error(pl.login_id, "OAuth client not configured")
        return _callback_page("Connection failed", "OAuth client not configured.")
    try:
        token_set = await connector.oauth.exchange_code(
            client_id=creds[0],
            client_secret=creds[1],
            code=code,
            redirect_uri=pl.redirect_uri,
            code_verifier=pl.verifier,
            state=pl.state,
        )
        inst = await _require_manager().complete_install(
            kind=pl.kind, token_set=token_set, requested_label=pl.requested_label
        )
    except Exception as e:  # exchange / identity / persistence failure
        logger.warning("connector oauth callback failed: %s", e)
        _login_mgr.mark_error(pl.login_id, str(e))
        return _callback_page("Connection failed", "Could not complete sign-in.")

    _login_mgr.mark_success(pl.login_id, inst["id"])
    return _callback_page(
        "Connected", f"{connector.display_name} ({inst['label']}) is connected."
    )


@router.get(
    "/oauth/status/{login_id}", response_model=ConnectorOAuthStatusResponse
)
async def oauth_status(login_id: str, _: str = Depends(verify_token)):
    pl = _login_mgr.get(login_id)
    if pl is None:
        raise HTTPException(status_code=404, detail="unknown or expired login")
    return ConnectorOAuthStatusResponse(
        status=pl.status.value,
        installation_id=pl.installation_id,
        message=pl.message,
    )


@router.post("/oauth/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def oauth_cancel(
    req: ConnectorOAuthCancelRequest, _: str = Depends(verify_token)
):
    _login_mgr.cancel(req.login_id)


# --- installation management ----------------------------------------------


@router.patch("/{installation_id}", response_model=ConnectorInstallationInfo)
async def update_installation(
    installation_id: str,
    req: UpdateConnectorRequest,
    _: str = Depends(verify_token),
):
    fields = req.model_dump(exclude_unset=True)
    try:
        row = await _require_manager().update_installation(installation_id, **fields)
    except ConnectorError as e:
        raise _http_error(e)
    return _to_info(row)


@router.delete("/{installation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_installation(installation_id: str, _: str = Depends(verify_token)):
    try:
        await _require_manager().delete_installation(installation_id)
    except ConnectorError as e:
        raise _http_error(e)


# --- internal routes (only the connector MCP subprocess calls these) -------


@router.get("/{installation_id}/token", response_model=ConnectorTokenResponse)
async def get_token(installation_id: str, _: str = Depends(verify_token)):
    try:
        out = await _require_manager().get_access_token(installation_id)
    except ConnectorError as e:
        raise _http_error(e)
    return ConnectorTokenResponse(**out)


@router.post(
    "/{installation_id}/mark-needs-reconnect",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def mark_needs_reconnect(
    installation_id: str,
    error_code: str = Query(default="invalid_grant"),
    _: str = Depends(verify_token),
):
    try:
        await _require_manager().mark_needs_reconnect(installation_id, error_code)
    except ConnectorError as e:
        raise _http_error(e)


# --- agent-scoped enablement ----------------------------------------------


@agent_router.get(
    "/{agent_id}/connectors", response_model=AgentConnectorsResponse
)
async def list_agent_connectors(agent_id: str, _: str = Depends(verify_token)):
    ids = await _require_manager().get_agent_connector_ids(agent_id)
    return AgentConnectorsResponse(installation_ids=ids)


@agent_router.put(
    "/{agent_id}/connectors", response_model=AgentConnectorsResponse
)
async def set_agent_connectors(
    agent_id: str,
    req: SetAgentConnectorsRequest,
    _: str = Depends(verify_token),
):
    try:
        ids = await _require_manager().replace_agent_connectors(
            agent_id, req.installation_ids
        )
    except ConnectorError as e:
        raise _http_error(e)
    return AgentConnectorsResponse(installation_ids=ids)


@agent_router.patch(
    "/{agent_id}/connectors/{installation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def toggle_agent_connector(
    agent_id: str,
    installation_id: str,
    req: ToggleAgentConnectorRequest,
    _: str = Depends(verify_token),
):
    try:
        await _require_manager().set_agent_connector(
            agent_id, installation_id, req.enabled
        )
    except ConnectorError as e:
        raise _http_error(e)
