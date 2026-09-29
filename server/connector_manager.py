"""ConnectorManager — business logic over the connector DB layer
(connectors.md §5.5). Mirrors AgentManager: routes wrap a singleton, errors
surface as ConnectorError → HTTP 400/404.

Owns the OAuth-token lifecycle: encrypt-at-rest, dedup upsert on
(kind, external account), and server-side refresh-on-near-expiry guarded by a
per-installation lock so concurrent MCP subprocesses refresh exactly once.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from .config import settings
from .connectors.base import ConnectorInstallation
from .connectors.custom import CustomConnector, resolve_connector
from .connectors.registry import all_connectors, get_connector
from .crypto import decrypt, encrypt, master_key
from .database import Database
from .monitor import Event as _MonEvent
from .monitor import record as _mon_record
from .oauth_providers import OAuthTokenSet

# Slugs reserved by routes / built-ins; a custom kind can't take these.
_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
_RESERVED_KINDS = {"oauth", "catalog", "custom"}

# Refresh when the access token expires within this window.
_REFRESH_SKEW_SECONDS = 300


class ConnectorError(Exception):
    """Connector business-rule violation; routes map this to 400/404."""


def _serialize_token_set(ts: OAuthTokenSet) -> str:
    return json.dumps(
        {
            "access_token": ts.access_token,
            "refresh_token": ts.refresh_token,
            "expires_at_epoch": ts.expires_at_epoch,
            "scopes": list(ts.scopes),
            "token_type": ts.token_type,
        },
        separators=(",", ":"),
    )


def _deserialize_token_set(blob: str) -> OAuthTokenSet:
    d = json.loads(blob)
    return OAuthTokenSet(
        access_token=d["access_token"],
        refresh_token=d.get("refresh_token"),
        expires_at_epoch=float(d.get("expires_at_epoch") or 0.0),
        scopes=list(d.get("scopes") or []),
        token_type=d.get("token_type") or "Bearer",
    )


def _expires_iso(epoch: float) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _env_client_creds(kind: str) -> tuple[str, str] | None:
    """OAuth client creds for a kind from env (the fallback when nothing is
    set in-app): OCTOPUS_<KIND>_OAUTH_CLIENT_ID / _SECRET."""
    cid = getattr(settings, f"{kind}_oauth_client_id", None)
    csec = getattr(settings, f"{kind}_oauth_client_secret", None)
    return (cid, csec) if cid and csec else None


class ConnectorManager:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, kind: str):
        """A connector by kind — built-in (code) or custom (DB), else None."""
        return await resolve_connector(self.db, kind)

    # --- custom (user-defined) connector definitions ---------------------

    async def create_custom_connector(
        self,
        *,
        kind: str,
        display_name: str,
        authorize_url: str,
        token_url: str,
        scopes: list[str],
        pkce: bool,
        api_base: str,
        client_id: str,
        client_secret: str,
    ) -> dict[str, Any]:
        kind = (kind or "").strip().lower()
        if not _KIND_RE.match(kind) or kind in _RESERVED_KINDS:
            raise ConnectorError(
                "kind must be a slug (lowercase letters, digits, - or _) and "
                "not a reserved word"
            )
        if get_connector(kind) is not None:
            raise ConnectorError(f"{kind!r} is a built-in connector")
        if await self.db.get_custom_connector(kind) is not None:
            raise ConnectorError(f"a custom connector {kind!r} already exists")
        now = datetime.now(UTC).isoformat()
        await self.db.save_custom_connector(
            kind=kind,
            display_name=display_name.strip() or kind,
            authorize_url=authorize_url.strip(),
            token_url=token_url.strip(),
            scopes=list(scopes),
            pkce=bool(pkce),
            api_base=api_base.strip(),
            now=now,
        )
        # Client creds reuse the in-app config store (same as built-ins).
        await self.set_client_creds(kind, client_id, client_secret)
        row = await self.db.get_custom_connector(kind)
        assert row is not None
        return row

    async def delete_custom_connector(self, kind: str) -> None:
        if await self.db.get_custom_connector(kind) is None:
            raise ConnectorError("custom connector not found")
        # Tear down everything tied to the kind so nothing is orphaned.
        await self.db.delete_connector_installations_by_kind(kind)
        await self.db.delete_connector_oauth_client(kind)
        await self.db.delete_custom_connector(kind)

    # --- OAuth client credentials (in-app config, DB-first then env) ------

    async def resolve_client_creds(self, kind: str) -> tuple[str, str] | None:
        """Effective (client_id, client_secret) for a kind: the in-app DB row
        wins, else the env fallback, else None (→ catalog shows unavailable).

        Keyed with the **master key**, not a user's DEK and no longer the
        install token: an OAuth client is this box's registration with the
        provider, which is why it has an env fallback at all
        (multi-tenancy.md §4).
        """
        row = await self.db.get_connector_oauth_client(kind)
        if row and row["client_id"] and row["client_secret_encrypted"]:
            return row["client_id"], decrypt(
                row["client_secret_encrypted"], master_key()
            )
        return _env_client_creds(kind)

    async def set_client_creds(
        self, kind: str, client_id: str, client_secret: str
    ) -> None:
        if await resolve_connector(self.db, kind) is None:
            raise ConnectorError(f"unknown connector kind: {kind}")
        now = datetime.now(UTC).isoformat()
        await self.db.set_connector_oauth_client(
            kind, client_id, encrypt(client_secret, master_key()), now
        )

    async def clear_client_creds(self, kind: str) -> bool:
        return await self.db.delete_connector_oauth_client(kind)

    async def client_config(self, kind: str, public_base: str) -> dict[str, Any]:
        """Non-secret view of a kind's OAuth client config for the UI: whether
        it's configured, the client_id, the source, and the redirect URI to
        register with the provider. `public_base` is derived from the caller's
        request so a browser-only user behind a tunnel gets the right URI."""
        if await resolve_connector(self.db, kind) is None:
            raise ConnectorError(f"unknown connector kind: {kind}")
        row = await self.db.get_connector_oauth_client(kind)
        client_id: str | None
        source: str | None
        if row and row["client_id"]:
            client_id, source = row["client_id"], "db"
        else:
            env = _env_client_creds(kind)
            client_id, source = (env[0], "env") if env else (None, None)
        return {
            "kind": kind,
            "configured": client_id is not None,
            "client_id": client_id,
            "source": source,
            "redirect_uri": f"{public_base}/api/connectors/oauth/callback",
        }

    # --- catalog + installations -----------------------------------------

    async def catalog(self) -> list[dict[str, Any]]:
        connectors = list(all_connectors()) + [
            CustomConnector(row) for row in await self.db.list_custom_connectors()
        ]
        out: list[dict[str, Any]] = []
        for c in connectors:
            out.append(
                {
                    "kind": c.kind,
                    "display_name": c.display_name,
                    "category": c.category,
                    "allows_multiple": c.allows_multiple,
                    "available": (await self.resolve_client_creds(c.kind))
                    is not None,
                    "scopes": list(getattr(c.oauth, "default_scopes", [])),
                    "custom": getattr(c, "is_custom", False),
                    "setup_url": getattr(c, "setup_url", "") or None,
                    "setup_steps": list(getattr(c, "setup_steps", [])),
                }
            )
        return out

    async def list_installations(
        self, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """This account's connected accounts. An installation *is* somebody's
        access to a third-party account, so this is the per-account half of a
        connector (multi-tenancy.md §5)."""
        return await self.db.load_connector_installations(user_id=user_id)

    async def get_installation(
        self, installation_id: str, user_id: str | None = None
    ) -> dict[str, Any] | None:
        return await self.db.get_connector_installation(installation_id, user_id)

    async def complete_install(
        self,
        *,
        kind: str,
        token_set: OAuthTokenSet,
        requested_label: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist a freshly-authorized installation, upserting on the
        provider account so re-auth of the same account overwrites.

        `user_id` comes from the pending login the *authenticated* `oauth/start`
        created, because the provider's redirect carries no bearer of ours
        (multi-tenancy.md §5). The upsert is scoped to it too: two accounts
        connecting the same GitHub account get one installation each, since the
        tokens are separately theirs.
        """
        connector = await resolve_connector(self.db, kind)
        if connector is None:
            raise ConnectorError(f"unknown connector kind: {kind}")
        external_id, identity_label = await connector.fetch_external_identity(
            token_set
        )
        label = requested_label or identity_label
        from . import deps

        secret_key = await deps.data_key_for(user_id)
        blob = encrypt(_serialize_token_set(token_set), secret_key)
        token_expires_at = _expires_iso(token_set.expires_at_epoch)

        existing = (
            await self.db.get_connector_installation_by_account(
                kind, external_id, user_id
            )
            if external_id
            else None
        )
        if existing is not None:
            iid = existing["id"]
            await self.db.update_connector_installation(
                iid,
                label=label,
                scopes=list(token_set.scopes),
                token_expires_at=token_expires_at,
                needs_reconnect=False,
                last_refresh_error_code=None,
                secret_encrypted=blob,
            )
        else:
            iid = uuid.uuid4().hex[:12]
            await self.db.save_connector_installation(
                installation_id=iid,
                kind=kind,
                label=label,
                auth_type="oauth",
                secret_encrypted=blob,
                created_at=datetime.now(UTC).isoformat(),
                external_account_id=external_id or None,
                scopes=list(token_set.scopes),
                token_expires_at=token_expires_at,
                user_id=user_id,
            )
        inst = await self.db.get_connector_installation(iid)
        assert inst is not None
        return inst

    async def update_installation(
        self, installation_id: str, user_id: str | None = None, **fields: Any
    ) -> dict[str, Any]:
        if await self.db.get_connector_installation(installation_id, user_id) is None:
            raise ConnectorError("connector installation not found")
        await self.db.update_connector_installation(installation_id, **fields)
        updated = await self.db.get_connector_installation(installation_id)
        assert updated is not None
        return updated

    async def delete_installation(
        self, installation_id: str, user_id: str | None = None
    ) -> None:
        # Resolved within the account first: a DELETE straight to the id would
        # remove anybody's, and report the same success either way.
        if await self.db.get_connector_installation(installation_id, user_id) is None:
            raise ConnectorError("connector installation not found")
        if not await self.db.delete_connector_installation(installation_id):
            raise ConnectorError("connector installation not found")

    async def mark_needs_reconnect(
        self,
        installation_id: str,
        error_code: str = "invalid_grant",
        user_id: str | None = None,
    ) -> None:
        if await self.db.get_connector_installation(installation_id, user_id) is None:
            raise ConnectorError("connector installation not found")
        await self.db.update_connector_installation(
            installation_id, needs_reconnect=True, last_refresh_error_code=error_code
        )
        # The other half of the Gmail case (§9 G0): the app knew the connector
        # was broken, but nothing counted how long it stayed that way.
        _mon_record(_MonEvent(
            kind="connector_reconnect_needed",
            ok=False,
            error_code=error_code,
            detail={"installation_id": installation_id},
        ))

    # --- token access (the internal /token route) ------------------------

    def _lock(self, installation_id: str) -> asyncio.Lock:
        lock = self._locks.get(installation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[installation_id] = lock
        return lock

    async def get_access_token(
        self, installation_id: str, user_id: str | None = None
    ) -> dict[str, Any]:
        """Return {access_token, expires_at_epoch}, refreshing server-side if
        near expiry. On refresh failure marks needs_reconnect and raises.

        The secret is keyed to the installation's *owner*, read off the row —
        so the boot-time refresh sweep, which passes no scope, decrypts the same
        blob a request does (multi-tenancy.md §4).
        """
        inst = await self.db.get_connector_installation(installation_id, user_id)
        if inst is None:
            raise ConnectorError("connector installation not found")

        from . import deps

        secret_key = await deps.data_key_for(inst.get("user_id"))
        async with self._lock(installation_id):
            blob = await self.db.get_connector_secret(installation_id)
            if blob is None:
                raise ConnectorError("connector secret missing")
            ts = _deserialize_token_set(decrypt(blob, secret_key))

            near_expiry = (
                ts.expires_at_epoch
                and ts.expires_at_epoch - time.time() < _REFRESH_SKEW_SECONDS
            )
            if near_expiry and ts.refresh_token:
                connector = await resolve_connector(self.db, inst["kind"])
                provider = getattr(connector, "oauth", None)
                if provider is None:
                    raise ConnectorError(f"no provider for kind {inst['kind']!r}")
                creds = await self.resolve_client_creds(inst["kind"])
                if creds is None:
                    raise ConnectorError(
                        f"{inst['kind']} OAuth client is not configured"
                    )
                try:
                    new_ts = await provider.refresh(
                        client_id=creds[0],
                        client_secret=creds[1],
                        refresh_token=ts.refresh_token,
                    )
                except Exception as e:  # provider/transport failure → reconnect
                    await self.db.update_connector_installation(
                        installation_id,
                        needs_reconnect=True,
                        last_refresh_error_code=getattr(e, "code", "refresh_failed"),
                    )
                    raise ConnectorError(f"token refresh failed: {e}") from e
                # Providers may omit the refresh token when the old one stands.
                if new_ts.refresh_token is None:
                    new_ts.refresh_token = ts.refresh_token
                ts = new_ts
                await self.db.update_connector_installation(
                    installation_id,
                    secret_encrypted=encrypt(_serialize_token_set(ts), secret_key),
                    token_expires_at=_expires_iso(ts.expires_at_epoch),
                    needs_reconnect=False,
                    last_refresh_error_code=None,
                )

            return {
                "access_token": ts.access_token,
                "expires_at_epoch": ts.expires_at_epoch,
            }

    # --- agent-scoped enablement -----------------------------------------

    async def get_agent_connector_ids(self, agent_id: str) -> list[str]:
        return await self.db.get_agent_connector_ids(agent_id)

    async def set_agent_connector(
        self,
        agent_id: str,
        installation_id: str,
        enabled: bool,
        user_id: str | None = None,
    ) -> None:
        if await self.db.get_connector_installation(installation_id, user_id) is None:
            raise ConnectorError("connector installation not found")
        await self.db.set_agent_connector(agent_id, installation_id, enabled)

    async def replace_agent_connectors(
        self,
        agent_id: str,
        installation_ids: list[str],
        user_id: str | None = None,
    ) -> list[str]:
        current = set(await self.db.get_agent_connector_ids(agent_id))
        target = set(installation_ids)
        for iid in target - current:
            if await self.db.get_connector_installation(iid, user_id) is None:
                raise ConnectorError(f"connector installation not found: {iid}")
            await self.db.set_agent_connector(agent_id, iid, True)
        for iid in current - target:
            await self.db.set_agent_connector(agent_id, iid, False)
        return sorted(target)

    async def enabled_installations_for_agent(
        self, agent_id: str
    ) -> list[ConnectorInstallation]:
        """The agent's enabled connectors as ConnectorInstallation views — what
        SessionManager passes to the backend at spawn time."""
        rows = await self.db.get_enabled_connectors_for_agent(agent_id)
        return [ConnectorInstallation.from_row(r) for r in rows]
