"""Turning a single-user install into its first account (multi-tenancy.md §9).

One operation, because the pieces have to happen together or not at all: an
install with a user whose secrets are still keyed to the retired token, or with
rows nobody owns, is worse than either the before or the after.

    create the account  ->  re-key its secrets  ->  adopt its rows
                        ->  remember where it already works

It is a one-way door on a live box, so the order is chosen for what a failure
leaves behind. The account is created first because everything else needs an
owner; the secrets are re-keyed before the rows are adopted because a failure
there must not leave rows pointing at a user whose credentials do not open;
and `extra_roots` is set last because it is the only part that is merely
convenient rather than correct.

Available exactly once: the route that calls this is refused the moment an
account exists, which is also when the legacy token it authenticates with stops
working. The two facts are the same fact.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from .config import settings
from .crypto import decrypt, encrypt
from .users import UserError, UserManager
from .workspace import paths_for

logger = logging.getLogger(__name__)

# The same three tables `token_rotation` re-keys, for the same reason: they are
# every ciphertext the key protects.
_SECRET_COLUMNS = (
    ("credential_secrets", "credential_id", "secret_encrypted"),
    ("connector_installation_secrets", "installation_id", "secret_encrypted"),
)

# And the install's own, which move to the **master key** instead: an OAuth
# client is this box's app registration with a provider, not a user's secret, so
# handing it to whoever happens to create the first account would be wrong in
# exactly the way that matters when a second one arrives (multi-tenancy.md §4).
_INSTALL_SECRET_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("connector_oauth_clients", "kind", "client_secret_encrypted"),
)


class BootstrapError(Exception):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


async def _rekey_secrets(db: Any, old_key: str, new_key: str) -> dict[str, int]:
    """Move every stored secret off the old key. All, or none.

    A user's secrets go to `new_key`, their own DEK; the install's own go to the
    master key, because they belong to the box rather than to the first person
    who claimed it.

    Read and re-key everything before writing anything, which is the lesson
    `token_rotation` already paid for: a row that will not decrypt must cost
    nothing rather than leave the rows before it on the new key and the rows
    after it on the old.
    """
    from .crypto import master_key

    await db._ensure_connected()
    counts: dict[str, int] = {}
    updates: list[tuple[str, str, str, str, str]] = []

    for tables, destination in (
        (_SECRET_COLUMNS, new_key),
        (_INSTALL_SECRET_COLUMNS, master_key()),
    ):
        for table, key_col, secret_col in tables:
            cursor = await db.conn.execute(
                f"SELECT {key_col}, {secret_col} FROM {table}"  # noqa: S608
            )
            counts[table] = 0
            for row_key, ciphertext in await cursor.fetchall():
                if not ciphertext:
                    continue
                try:
                    plaintext = decrypt(ciphertext, old_key)
                except ValueError as exc:
                    raise BootstrapError(
                        f"{table}.{key_col}={row_key!r} could not be decrypted "
                        f"with the current token, so nothing was changed "
                        f"({exc}). Fix or delete that row and try again."
                    ) from exc
                updates.append(
                    (
                        table,
                        key_col,
                        secret_col,
                        row_key,
                        encrypt(plaintext, destination),
                    )
                )
                counts[table] += 1

    for table, key_col, secret_col, row_key, ciphertext in updates:
        await db.conn.execute(
            f"UPDATE {table} SET {secret_col} = ? WHERE {key_col} = ?",  # noqa: S608
            (ciphertext, row_key),
        )
    await db.conn.commit()
    return counts


async def _existing_working_dirs(db: Any) -> list[str]:
    """Every distinct directory this install's sessions already work in.

    They become the first account's `extra_roots`, because confining them to a
    workspace that did not exist when they were created would break live
    sessions on the first turn after the upgrade — including, on the machine
    this was written on, the ones working on Octopus itself.
    """
    rows = await db.load_sessions(include_archived=True)
    roots: list[str] = []
    for row in rows:
        raw = (row.get("working_dir") or "").strip()
        if not raw:
            continue
        path = str(Path(raw).expanduser())
        if path not in roots:
            roots.append(path)
    return roots


def _move_agent_memory(user_id: str) -> int:
    """Move the install's agent memory under the new account.

    A move rather than a copy: two copies of an agent's memory diverging is
    worse than either, and the destination is on the same filesystem. Skipped
    silently when there is nothing there, which is the fresh-install case.
    """
    legacy = Path(settings.agents_dir).expanduser()
    if not legacy.is_dir():
        return 0
    destination = paths_for(user_id).agents
    destination.mkdir(parents=True, exist_ok=True)
    moved = 0
    for entry in legacy.iterdir():
        target = destination / entry.name
        if target.exists():
            continue
        shutil.move(str(entry), str(target))
        moved += 1
    return moved


async def bootstrap_first_account(
    db: Any,
    *,
    username: str,
    password: str,
    adopt: bool = True,
    session_manager: Any = None,
) -> dict[str, Any]:
    """Create the install's first account and hand it everything.

    Returns a summary of what moved, so the operator sees the upgrade rather
    than trusting it.

    `session_manager` is the live one, when there is one. Rows are only half
    the install: the sessions already loaded into memory carry the owner too,
    and a server that is not restarted would go on serving them as unowned.
    """
    users = UserManager(db)
    if await db.count_users() > 0:
        raise BootstrapError(
            "This install already has an account", status_code=409
        )

    try:
        user = await users.create_user(
            username=username, password=password, is_admin=True
        )
    except UserError as exc:
        raise BootstrapError(exc.message, status_code=exc.status_code) from exc

    summary: dict[str, Any] = {"user_id": user["id"], "username": user["username"]}

    # Secrets before rows: a failure here must not leave rows pointing at an
    # account whose credentials do not open.
    summary["reencrypted"] = await _rekey_secrets(
        db, settings.auth_token, users.data_key(user)
    )

    if adopt:
        summary["adopted"] = await db.adopt_orphan_rows(user["id"])
        roots = await _existing_working_dirs(db)
        if roots:
            await db.update_user_field(user["id"], extra_roots=json.dumps(roots))
        summary["extra_roots"] = roots
        summary["agent_memory_moved"] = _move_agent_memory(user["id"])
        paths_for(user["id"]).ensure()
        if session_manager is not None:
            summary["live_sessions_adopted"] = session_manager.adopt_orphan_sessions(
                user["id"]
            )

    logger.info("bootstrapped the first account: %s", summary)
    return summary
