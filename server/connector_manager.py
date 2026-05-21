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
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .config import settings
from .connectors.base import ConnectorInstallation
from .connectors.registry import all_connectors, get_connector
from .crypto import decrypt, encrypt
from .database import Database
from .oauth_providers import OAuthTokenSet

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
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def connector_available(connector: Any) -> bool:
    """A kind is installable only when both OAuth client creds are present.
    Real providers carry `client_id`/`client_secret` (from settings); a kind
    with either missing is greyed out in the catalog."""
    cid = getattr(connector.oauth, "client_id", None)
    csec = getattr(connector.oauth, "client_secret", None)
    return bool(cid and csec)


class ConnectorManager:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._locks: dict[str, asyncio.Lock] = {}

    # --- catalog + installations -----------------------------------------

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "kind": c.kind,
                "display_name": c.display_name,
                "category": c.category,
                "allows_multiple": c.allows_multiple,
                "available": connector_available(c),
            }
            for c in all_connectors()
        ]

    async def list_installations(self) -> list[dict[str, Any]]:
        return await self.db.load_connector_installations()

    async def get_installation(self, installation_id: str) -> dict[str, Any] | None:
        return await self.db.get_connector_installation(installation_id)

    async def complete_install(
        self,
        *,
        kind: str,
        token_set: OAuthTokenSet,
        requested_label: str | None = None,
    ) -> dict[str, Any]:
        """Persist a freshly-authorized installation, upserting on the
        provider account so re-auth of the same account overwrites."""
        connector = get_connector(kind)
        if connector is None:
            raise ConnectorError(f"unknown connector kind: {kind}")
        external_id, identity_label = await connector.fetch_external_identity(
            token_set
        )
        label = requested_label or identity_label
        blob = encrypt(_serialize_token_set(token_set), settings.auth_token)
        token_expires_at = _expires_iso(token_set.expires_at_epoch)

        existing = (
            await self.db.get_connector_installation_by_account(kind, external_id)
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
                created_at=datetime.now(timezone.utc).isoformat(),
                external_account_id=external_id or None,
                scopes=list(token_set.scopes),
                token_expires_at=token_expires_at,
            )
        inst = await self.db.get_connector_installation(iid)
        assert inst is not None
        return inst

    async def update_installation(
        self, installation_id: str, **fields: Any
    ) -> dict[str, Any]:
        if await self.db.get_connector_installation(installation_id) is None:
            raise ConnectorError("connector installation not found")
        await self.db.update_connector_installation(installation_id, **fields)
        updated = await self.db.get_connector_installation(installation_id)
        assert updated is not None
        return updated

    async def delete_installation(self, installation_id: str) -> None:
        if not await self.db.delete_connector_installation(installation_id):
            raise ConnectorError("connector installation not found")

    async def mark_needs_reconnect(
        self, installation_id: str, error_code: str = "invalid_grant"
    ) -> None:
        if await self.db.get_connector_installation(installation_id) is None:
            raise ConnectorError("connector installation not found")
        await self.db.update_connector_installation(
            installation_id, needs_reconnect=True, last_refresh_error_code=error_code
        )

    # --- token access (the internal /token route) ------------------------

    def _lock(self, installation_id: str) -> asyncio.Lock:
        lock = self._locks.get(installation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[installation_id] = lock
        return lock

    async def get_access_token(self, installation_id: str) -> dict[str, Any]:
        """Return {access_token, expires_at_epoch}, refreshing server-side if
        near expiry. On refresh failure marks needs_reconnect and raises."""
        inst = await self.db.get_connector_installation(installation_id)
        if inst is None:
            raise ConnectorError("connector installation not found")

        async with self._lock(installation_id):
            blob = await self.db.get_connector_secret(installation_id)
            if blob is None:
                raise ConnectorError("connector secret missing")
            ts = _deserialize_token_set(decrypt(blob, settings.auth_token))

            near_expiry = (
                ts.expires_at_epoch
                and ts.expires_at_epoch - time.time() < _REFRESH_SKEW_SECONDS
            )
            if near_expiry and ts.refresh_token:
                connector = get_connector(inst["kind"])
                provider = getattr(connector, "oauth", None)
                if provider is None:
                    raise ConnectorError(f"no provider for kind {inst['kind']!r}")
                try:
                    new_ts = await provider.refresh(ts.refresh_token)
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
                    secret_encrypted=encrypt(
                        _serialize_token_set(ts), settings.auth_token
                    ),
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
        self, agent_id: str, installation_id: str, enabled: bool
    ) -> None:
        if await self.db.get_connector_installation(installation_id) is None:
            raise ConnectorError("connector installation not found")
        await self.db.set_agent_connector(agent_id, installation_id, enabled)

    async def replace_agent_connectors(
        self, agent_id: str, installation_ids: list[str]
    ) -> list[str]:
        current = set(await self.db.get_agent_connector_ids(agent_id))
        target = set(installation_ids)
        for iid in target - current:
            if await self.db.get_connector_installation(iid) is None:
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
