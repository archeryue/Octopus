"""Resolving the credential a turn runs under, and refreshing it when it is near expiry.

One slice of `SessionManager`, which was a single 4,017-line class with 84
methods (docs/plans/polish-2026-09.md §3 A1). Split by responsibility; the
methods are unchanged and still reached through one `session_manager` object,
so no call site moved.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from typing import Any

from ..config import settings
from ..crypto import decrypt, encrypt
from ..harness import HarnessCredential
from ..models import CredentialStatus
from ..oauth_errors import RefreshErrorCode
from ..oauth_providers import OAuthTokenSet, get_provider
from .base import (
    Session,
    SessionManagerBase,
    logger,
)


class CredentialsMixin(SessionManagerBase):

    async def _resolve_credential(
        self, session: Session, agent: dict[str, Any] | None, harness
    ) -> HarnessCredential | None:
        """Look up the effective credential and resolve it for `harness`.

        Effective id is `session.credential_id` if set, else the agent's
        (agent-refactor.md §5.2 / decision #2). The harness's profile
        `credential_style` picks the shape: `env_secret` decrypts the secret
        (refreshing an OAuth bundle if near expiry); `home_dir` resolves the
        CODEX_HOME directory. Returns None when nothing's attached / resolvable.
        """
        cred_id = session.credential_id or (
            agent.get("credential_id") if agent else None
        )
        return await self.resolve_credential_by_id(
            cred_id, style=harness.profile.credential_style, context=f"session {session.id}"
        )

    async def resolve_credential_by_id(
        self,
        cred_id: str | None,
        *,
        style: str = "env_secret",
        context: str = "",
        require_auth: bool = True,
    ) -> HarnessCredential | None:
        """Resolve a credential id into a `HarnessCredential` of the shape the
        harness needs (`style`). Returns None when there's no id, the row is
        missing/needs_reconnect, or it can't be resolved — the caller then runs
        with whatever auth the CLI finds on its own. `context` labels log lines.

        - ``home_dir`` (Codex): the credential is directory-backed; its dir is
          deterministic (`<codex_home_dir>/<credential_id>/`), so we resolve it
          with no DB read and require a completed login (auth.json present).
        - ``env_secret`` (Claude): decrypt the secret; refresh an OAuth bundle
          if near expiry, else use the long-lived key as-is.

        `require_auth=False` resolves the credential only for locating on-disk
        artifacts (e.g. fork transcript copy/cleanup), NOT for making API calls:
        a directory-backed credential returns its home dir even with a
        missing/revoked `auth.json` (the rollout still lives there and must be
        cleaned up — Vera review), and a secret-backed credential returns None
        (its transcripts aren't keyed by credential, so no home to locate)."""
        if not cred_id:
            return None

        if style == "home_dir":
            from ..codex_login import codex_home_for

            home = codex_home_for(cred_id)
            if require_auth and not os.path.exists(os.path.join(home, "auth.json")):
                return None  # inherit the host default ~/.codex (option A)
            return HarnessCredential(backend="codex", auth_type="oauth", home_dir=home)

        if not require_auth:
            # Secret-backed (Claude): no per-credential on-disk artifact store to
            # locate, so artifact copy/cleanup needs no credential.
            return None

        if self.db is None:
            return None
        row = await self.db.get_credential(cred_id)
        if row is None:
            logger.warning(
                "%s references missing credential %s; running without auth override",
                context or "caller",
                cred_id,
            )
            return None
        if row.get("needs_reconnect"):
            logger.warning(
                "Credential %s is in needs_reconnect state (%s); running without auth override",
                cred_id,
                row.get("last_refresh_error_code"),
            )
            return None
        try:
            plaintext = decrypt(row["secret_encrypted"], settings.auth_token)
        except ValueError:
            logger.warning(
                "Could not decrypt credential %s (wrong auth token?); running without auth override",
                cred_id,
            )
            return None

        # OAuth-token bundle (Pro/Max subscriber path): the secret is a
        # JSON blob, not a bare key. Refresh if close to expiry, then use
        # the access_token as the runtime secret.
        if row["auth_type"] == "oauth" and plaintext.startswith("{"):
            access_token = await self._refresh_oauth_if_needed(
                credential_id=cred_id,
                backend=row["backend"],
                bundle_json=plaintext,
            )
            if access_token is None:
                return None
            return HarnessCredential(
                backend=row["backend"],
                auth_type="oauth",
                secret=access_token,
            )

        # Either auth_type=api_key OR legacy auth_type=oauth where the
        # stored secret is the long-lived sk-ant- key from mint_api_key.
        # Both flow through ANTHROPIC_API_KEY at the backend.
        return HarnessCredential(
            backend=row["backend"],
            auth_type="api_key",
            secret=plaintext,
        )

    async def _refresh_oauth_if_needed(
        self,
        *,
        credential_id: str,
        backend: str,
        bundle_json: str,
    ) -> str | None:
        """Return a usable access_token for an OAuth-bundle credential.

        Parses the stored bundle. If the access_token is still fresh,
        returns it as-is. Otherwise hits the provider's refresh endpoint,
        persists the new bundle (DB write), and returns the new
        access_token.

        On unrecoverable refresh failure (refresh_token expired/reused/etc),
        marks the credential needs_reconnect with the right error code so
        the frontend can prompt re-login, and returns None.
        """
        try:
            bundle = json.loads(bundle_json)
        except json.JSONDecodeError:
            logger.warning(
                "Credential %s: stored OAuth bundle isn't valid JSON",
                credential_id,
            )
            return None

        access_token = bundle.get("access_token")
        refresh_token = bundle.get("refresh_token")
        expires_at_epoch = bundle.get("expires_at_epoch", 0)
        if not isinstance(access_token, str):
            logger.warning(
                "Credential %s: OAuth bundle missing access_token",
                credential_id,
            )
            return None

        if (
            isinstance(expires_at_epoch, (int, float))
            and expires_at_epoch - time.time() > self._OAUTH_REFRESH_LEEWAY_SEC
        ):
            return access_token

        if not isinstance(refresh_token, str) or not refresh_token:
            # Can't refresh — mark needs_reconnect so the user knows.
            await self._mark_needs_reconnect(
                credential_id, RefreshErrorCode.refresh_token_other
            )
            return None

        try:
            provider = get_provider(backend)
        except KeyError:
            logger.warning(
                "Credential %s: unknown backend %r, can't refresh",
                credential_id,
                backend,
            )
            return None

        try:
            new_ts: OAuthTokenSet = await provider.refresh_access_token(refresh_token)
        except RuntimeError as e:
            code = self._classify_refresh_error(str(e))
            logger.warning(
                "Credential %s: refresh failed (%s): %s", credential_id, code.value, e
            )
            await self._mark_needs_reconnect(credential_id, code)
            return None
        except Exception:
            logger.exception(
                "Credential %s: unexpected refresh error", credential_id
            )
            await self._mark_needs_reconnect(
                credential_id, RefreshErrorCode.unknown
            )
            return None

        new_bundle = {
            "access_token": new_ts.access_token,
            "refresh_token": new_ts.refresh_token,
            "expires_at_epoch": new_ts.expires_at_epoch,
            "scopes": list(new_ts.scopes),
            "token_type": new_ts.token_type,
        }
        secret_encrypted = encrypt(
            json.dumps(new_bundle, separators=(",", ":")),
            settings.auth_token,
        )
        token_expires_at = datetime.fromtimestamp(
            new_ts.expires_at_epoch, tz=UTC
        ).isoformat()
        await self.db.update_credential(
            credential_id,
            secret_encrypted=secret_encrypted,
            token_expires_at=token_expires_at,
            needs_reconnect=False,
            last_refresh_error_code=None,
        )
        return new_ts.access_token

    async def _mark_needs_reconnect(
        self, credential_id: str, code: RefreshErrorCode
    ) -> None:
        if self.db is None:
            return
        await self.db.update_credential(
            credential_id,
            status=CredentialStatus.needs_reconnect.value,
            needs_reconnect=True,
            last_refresh_error_code=code.value,
        )

    @staticmethod
    def _classify_refresh_error(msg: str) -> RefreshErrorCode:
        lower = msg.lower()
        if "expired" in lower:
            return RefreshErrorCode.refresh_token_expired
        if "reused" in lower or "already used" in lower:
            return RefreshErrorCode.refresh_token_reused
        if "invalid_grant" in lower or "invalidated" in lower or "revoked" in lower:
            return RefreshErrorCode.refresh_token_invalidated
        if (
            "network" in lower
            or "timeout" in lower
            or "connection" in lower
        ):
            return RefreshErrorCode.network_error
        if "refresh endpoint returned" in lower:
            return RefreshErrorCode.refresh_token_other
        return RefreshErrorCode.unknown
