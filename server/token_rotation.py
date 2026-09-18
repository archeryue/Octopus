"""Rotating `OCTOPUS_AUTH_TOKEN` in one operation (docs/plans/token-rotation.md).

The token is both the credential clients present and the key
`server/crypto.py` derives to encrypt every secret in the database, so
changing it by hand — edit the env file, restart — performs half the job and
silently breaks the rest: stored credentials stop decrypting, and open clients
reconnect forever against a token that is no longer accepted.

This module does all of it, in an order chosen so that a failure anywhere
leaves the system consistent.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import settings
from .crypto import decrypt, encrypt

if TYPE_CHECKING:
    from .database import Database

logger = logging.getLogger(__name__)

ENV_KEY = "OCTOPUS_AUTH_TOKEN"

# Short enough not to be annoying, long enough that "changeme" and a
# four-letter word don't pass. The point of a rotation is usually that the old
# one was too easy.
MIN_TOKEN_LENGTH = 12

# A token rides in a URL query (`/ws?token=…`) and in a cookie value
# (`octopus_app_token`), so these characters would either need escaping or
# silently truncate it.
_FORBIDDEN = re.compile(r"[\s;,\"'\\]")

# Every ciphertext the token protects: (table, key column, secret column).
_SECRET_COLUMNS = (
    ("credential_secrets", "credential_id", "secret_encrypted"),
    ("connector_installation_secrets", "installation_id", "secret_encrypted"),
    ("connector_oauth_clients", "kind", "client_secret_encrypted"),
)


class TokenRotationError(Exception):
    """Something that stops a rotation, with the status code to answer with."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class RotationResult:
    """What a rotation actually did, so the UI can say it plainly."""

    env_files: list[str] = field(default_factory=list)
    reencrypted: dict[str, int] = field(default_factory=dict)


def validate_new_token(new_token: str) -> str:
    """The new token, or a refusal explaining what's wrong with it."""
    token = (new_token or "").strip()
    if not token:
        raise TokenRotationError("The new token can't be empty")
    if len(token) < MIN_TOKEN_LENGTH:
        raise TokenRotationError(
            f"The new token must be at least {MIN_TOKEN_LENGTH} characters"
        )
    if _FORBIDDEN.search(token):
        raise TokenRotationError(
            "The new token can't contain spaces, quotes, backslashes, "
            "semicolons or commas — it travels in a URL query and a cookie"
        )
    if token == "changeme":
        raise TokenRotationError("Pick something other than the default token")
    if token == settings.auth_token:
        raise TokenRotationError("That's already the current token")
    return token


def env_files_defining_token() -> list[Path]:
    """The env files that actually set `OCTOPUS_AUTH_TOKEN`.

    Both are updated when both exist: one is handed to the service by systemd,
    the other is what pydantic reads from the working directory. Updating only
    one resurrects the old token on the next restart — a server that can't
    decrypt its own database.
    """
    override = os.environ.get("OCTOPUS_ENV_FILE")
    if override:
        # An explicit pointer is the whole list, never a first guess: the
        # fallbacks below are real files on a real machine, and a test (or a
        # script) that names its own must not be able to reach them. This
        # module rewrites secrets; the blast radius of a wrong guess is
        # someone's live config.
        candidates = [Path(override)]
    else:
        candidates = [
            # What pydantic reads (relative to the working directory)…
            Path.cwd() / ".env",
            # …and what systemd hands the service, which is the repo's own.
            Path(__file__).resolve().parent.parent / ".env",
        ]
    found: list[Path] = []
    for path in candidates:
        if path is None or not path.is_file():
            continue
        resolved = path.resolve()
        if resolved in found:
            continue
        try:
            text = resolved.read_text()
        except OSError:
            continue
        if re.search(rf"^{ENV_KEY}=", text, re.M):
            found.append(resolved)
    return found


def _rewrite_env_file(path: Path, token: str) -> None:
    """Replace the token line in `path`, atomically, leaving everything else
    exactly as it was."""
    original = path.read_text()
    updated = re.sub(
        rf"^{ENV_KEY}=.*$", f"{ENV_KEY}={token}", original, flags=re.M
    )
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".env-rotate-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(updated)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except Exception:
        # A half-written temp file is litter, not a config.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


async def rotate_auth_token(
    db: "Database",
    new_token: str,
    *,
    session_mgr: Any | None = None,
) -> RotationResult:
    """Change the access token everywhere it lives, or change nothing.

    Order is the whole design (docs/plans/token-rotation.md §2): persist the
    new token to disk first (a filesystem failure aborts with nothing
    changed), re-encrypt the database second (a failure there restores the
    files), swap the live setting third, and only then drop the processes
    carrying the old token in their environment.
    """
    old_token = settings.auth_token
    token = validate_new_token(new_token)

    paths = env_files_defining_token()
    if not paths:
        raise TokenRotationError(
            f"No env file defines {ENV_KEY}, so a new token couldn't survive a "
            f"restart. Add it to a .env file first.",
            status_code=409,
        )

    # 1. Disk, remembering enough to put it back.
    previous: dict[Path, str] = {}
    try:
        for path in paths:
            previous[path] = path.read_text()
            _rewrite_env_file(path, token)
    except OSError as exc:
        _restore(previous)
        raise TokenRotationError(
            f"Could not write {exc.filename or 'the env file'}: {exc.strerror}",
            status_code=500,
        )

    # 2. Every secret, in one transaction.
    try:
        result = RotationResult(env_files=[str(p) for p in paths])
        result.reencrypted = await _reencrypt_secrets(db, old_token, token)
    except Exception as exc:
        _restore(previous)
        if isinstance(exc, TokenRotationError):
            raise
        raise TokenRotationError(
            f"Re-encrypting the stored secrets failed, so nothing was changed: {exc}",
            status_code=500,
        )

    # 3. The live setting. Everything after this point is best-effort cleanup.
    settings.auth_token = token
    logger.info(
        "auth token rotated; rewrote %s, re-encrypted %s",
        ", ".join(result.env_files),
        result.reencrypted,
    )

    # 4. Processes that were handed the old token at spawn.
    await _drop_stale_processes(session_mgr)
    return result


def _restore(previous: dict[Path, str]) -> None:
    for path, text in previous.items():
        try:
            path.write_text(text)
        except OSError:
            logger.exception("could not restore %s after a failed rotation", path)


async def _reencrypt_secrets(
    db: "Database", old_token: str, new_token: str
) -> dict[str, int]:
    """Re-key every stored secret. All of them, or none.

    A row that won't decrypt with the old token aborts the rotation instead of
    being skipped: a half-rotated database is one where some secrets are keyed
    to a token nobody has any more.
    """
    await db._ensure_connected()
    counts: dict[str, int] = {}

    # Read and re-key everything BEFORE writing anything: a row that won't
    # decrypt then costs nothing, instead of leaving the rows before it keyed
    # to the new token and the rows after it keyed to the old one. (The
    # connection's implicit transaction is not enough on its own — this ran
    # halfway once, and the test that caught it is
    # `test_a_secret_that_cannot_be_decrypted_changes_nothing`.)
    updates: list[tuple[str, str, str, str, str]] = []
    for table, key_col, secret_col in _SECRET_COLUMNS:
        cursor = await db._conn.execute(f"SELECT {key_col}, {secret_col} FROM {table}")
        rows = await cursor.fetchall()
        counts[table] = len(rows)
        for key, ciphertext in rows:
            try:
                plaintext = decrypt(ciphertext, old_token)
            except ValueError as exc:
                raise TokenRotationError(
                    f"{table}.{key_col}={key!r} could not be decrypted with the "
                    f"current token, so nothing was changed ({exc}). Delete that "
                    f"row and re-add the credential, then rotate again.",
                    status_code=409,
                )
            updates.append(
                (table, key_col, secret_col, key, encrypt(plaintext, new_token))
            )

    try:
        for table, key_col, secret_col, key, ciphertext in updates:
            await db._conn.execute(
                f"UPDATE {table} SET {secret_col} = ? WHERE {key_col} = ?",
                (ciphertext, key),
            )
        await db._conn.commit()
    except Exception:
        await db._conn.rollback()
        raise
    return counts


async def _drop_stale_processes(session_mgr: Any | None) -> None:
    """Held CLI processes and application backends were handed the old token
    at spawn — the MCP children call back with `OCTOPUS_AUTH_TOKEN`, and an
    app backend's `OCTOPUS_APP_TOKEN` is derived from the master token. Both
    respawn on demand."""
    if session_mgr is not None:
        try:
            await session_mgr.stop_all_held_processes()
        except Exception:
            logger.exception("could not stop held CLI processes after rotation")
    try:
        from .app_backends import backend_supervisor

        await backend_supervisor.stop_all()
    except Exception:
        logger.exception("could not stop application backends after rotation")
