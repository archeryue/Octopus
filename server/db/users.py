"""Accounts, invites and bearer tokens.

One slice of the persistence layer (§3 A4's one-file-per-subject split). The
rows here are the control plane: who exists, who may create an account, and
which bearers are currently good for which account.

Nothing in this file decides policy — no password is hashed here, no token is
minted here, no expiry is judged here. That lives in `server/users.py`, so the
rules exist once and this file stays a set of statements about tables.
"""

from __future__ import annotations

from typing import Any

from .base import DatabaseBase


def _row_to_user(row: Any) -> dict[str, Any]:
    return {
        "id": row[0],
        "username": row[1],
        "password_hash": row[2],
        "dek_wrapped": row[3],
        "is_admin": bool(row[4]),
        "extra_roots": row[5],
        "created_at": row[6],
        "disabled_at": row[7],
    }


_USER_COLUMNS = (
    "id, username, password_hash, dek_wrapped, is_admin, extra_roots, "
    "created_at, disabled_at"
)


class UsersMixin(DatabaseBase):
    # -- users --------------------------------------------------------------

    async def create_user(
        self,
        *,
        user_id: str,
        username: str,
        password_hash: str,
        created_at: str,
        is_admin: bool = False,
        dek_wrapped: str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_connected()
        await self.conn.execute(
            "INSERT INTO users (id, username, password_hash, dek_wrapped, "
            " is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, password_hash, dek_wrapped, int(is_admin), created_at),
        )
        await self.conn.commit()
        created = await self.get_user(user_id)
        assert created is not None  # just inserted
        return created

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self.conn.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return _row_to_user(row) if row else None

    async def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        """Case-insensitively, because the column is NOCASE and a login that
        only matched the exact casing would reject the right person."""
        await self._ensure_connected()
        cursor = await self.conn.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        )
        row = await cursor.fetchone()
        return _row_to_user(row) if row else None

    async def list_users(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self.conn.execute(
            f"SELECT {_USER_COLUMNS} FROM users ORDER BY created_at"
        )
        return [_row_to_user(r) for r in await cursor.fetchall()]

    async def count_users(self) -> int:
        return await self._count("SELECT COUNT(*) FROM users")

    async def update_user_field(self, user_id: str, **fields: Any) -> None:
        """Set named columns. Same shape as `update_session_field` — the
        caller names what it means and the SQL is assembled once."""
        if not fields:
            return
        await self._ensure_connected()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        await self.conn.execute(
            f"UPDATE users SET {assignments} WHERE id = ?",
            (*fields.values(), user_id),
        )
        await self.conn.commit()

    # -- invites ------------------------------------------------------------

    async def create_invite(
        self,
        *,
        code: str,
        created_by: str | None,
        created_at: str,
        expires_at: str | None = None,
        max_uses: int = 1,
    ) -> dict[str, Any]:
        await self._ensure_connected()
        await self.conn.execute(
            "INSERT INTO invites (code, created_by, created_at, expires_at, "
            " max_uses) VALUES (?, ?, ?, ?, ?)",
            (code, created_by, created_at, expires_at, max_uses),
        )
        await self.conn.commit()
        found = await self.get_invite(code)
        assert found is not None
        return found

    async def get_invite(self, code: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self.conn.execute(
            "SELECT code, created_by, created_at, expires_at, max_uses, "
            " used_count, revoked_at FROM invites WHERE code = ?",
            (code,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "code": row[0],
            "created_by": row[1],
            "created_at": row[2],
            "expires_at": row[3],
            "max_uses": row[4],
            "used_count": row[5],
            "revoked_at": row[6],
        }

    async def list_invites(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self.conn.execute("SELECT code FROM invites ORDER BY created_at")
        codes = [r[0] for r in await cursor.fetchall()]
        out = []
        for code in codes:
            invite = await self.get_invite(code)
            if invite:
                out.append(invite)
        return out

    async def claim_invite(self, code: str) -> bool:
        """Consume one use, atomically.

        The `used_count < max_uses` lives in the UPDATE rather than in a
        read-then-write, so two registrations racing for the last use of a code
        cannot both win — SQLite settles it and the loser sees 0 rows changed.
        """
        await self._ensure_connected()
        cursor = await self.conn.execute(
            "UPDATE invites SET used_count = used_count + 1 "
            " WHERE code = ? AND revoked_at IS NULL AND used_count < max_uses",
            (code,),
        )
        await self.conn.commit()
        return (cursor.rowcount or 0) > 0

    async def revoke_invite(self, code: str, *, at: str) -> None:
        await self._ensure_connected()
        await self.conn.execute(
            "UPDATE invites SET revoked_at = ? WHERE code = ? AND revoked_at IS NULL",
            (at, code),
        )
        await self.conn.commit()

    # -- tokens -------------------------------------------------------------

    async def store_token(
        self,
        *,
        token_hash: str,
        user_id: str,
        kind: str,
        created_at: str,
        expires_at: str | None = None,
        label: str | None = None,
    ) -> None:
        await self._ensure_connected()
        await self.conn.execute(
            "INSERT INTO auth_tokens (token_hash, user_id, kind, label, "
            " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (token_hash, user_id, kind, label, created_at, expires_at),
        )
        await self.conn.commit()

    async def get_token(self, token_hash: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self.conn.execute(
            "SELECT token_hash, user_id, kind, label, created_at, expires_at, "
            " last_seen_at, revoked_at FROM auth_tokens WHERE token_hash = ?",
            (token_hash,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "token_hash": row[0],
            "user_id": row[1],
            "kind": row[2],
            "label": row[3],
            "created_at": row[4],
            "expires_at": row[5],
            "last_seen_at": row[6],
            "revoked_at": row[7],
        }

    async def list_tokens(self, user_id: str) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self.conn.execute(
            "SELECT token_hash FROM auth_tokens WHERE user_id = ? "
            " AND revoked_at IS NULL ORDER BY created_at",
            (user_id,),
        )
        hashes = [r[0] for r in await cursor.fetchall()]
        out = []
        for h in hashes:
            token = await self.get_token(h)
            if token:
                out.append(token)
        return out

    async def touch_token(self, token_hash: str, *, at: str) -> None:
        """Record that a bearer was just used.

        Deliberately not committed on its own — this fires on *every*
        authenticated request, and a commit per request would put a disk write
        in front of every call. It rides the next write's transaction, which is
        the same bargain `_maybe_flush` makes for messages.
        """
        await self._ensure_connected()
        await self.conn.execute(
            "UPDATE auth_tokens SET last_seen_at = ? WHERE token_hash = ?",
            (at, token_hash),
        )
        self._dirty = True

    async def revoke_token(self, token_hash: str, *, at: str) -> None:
        await self._ensure_connected()
        await self.conn.execute(
            "UPDATE auth_tokens SET revoked_at = ? WHERE token_hash = ? "
            " AND revoked_at IS NULL",
            (at, token_hash),
        )
        await self.conn.commit()

    async def revoke_all_tokens(self, user_id: str, *, at: str, keep: str | None = None) -> int:
        """Sign a user out everywhere. `keep` spares the caller's own bearer,
        which is what "change my password" wants: other devices out, this one
        still in."""
        await self._ensure_connected()
        sql = (
            "UPDATE auth_tokens SET revoked_at = ? WHERE user_id = ? "
            " AND revoked_at IS NULL"
        )
        params: list[Any] = [at, user_id]
        if keep is not None:
            sql += " AND token_hash != ?"
            params.append(keep)
        cursor = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cursor.rowcount or 0
