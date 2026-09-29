"""Accounts: passwords, bearers, invites — the rules, in one place.

`server/db/users.py` stores rows; this decides what a row is allowed to be.
Everything policy-shaped lives here so there is exactly one answer to "is this
password good enough", "is this bearer still valid", "may this code be used",
rather than one answer per call site (multi-tenancy.md §3).

Two choices worth stating, because both are load-bearing and neither is
obvious:

**scrypt from the standard library**, not argon2id. argon2id would be the
textbook pick, and it would add a dependency for a difference that does not
matter at this scale: scrypt is memory-hard, `hashlib` has it, and the stored
format carries its own parameters so the cost can be raised later without
invalidating a single existing password. A dependency that has to be installed,
pinned, audited and kept current is not free, and this is the kind of place
where "already in the standard library" wins.

**Only the digest of a bearer is stored.** The token is 256 bits of
`secrets.token_urlsafe`, so a plain SHA-256 is enough — there is no dictionary
to attack, which is what a slow KDF defends against. What it buys is that
reading the table hands over nothing usable, and under this version's isolation
model (§2) that table is readable by anyone who can run an agent.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .crypto import new_dek, unwrap_dek, wrap_dek

# scrypt cost. ~17 MB and tens of milliseconds per attempt — enough to make
# offline guessing expensive, little enough that a login feels instant.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32

SESSION_TTL = timedelta(days=30)

_USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,31}$")
MIN_PASSWORD_LENGTH = 8

# Names that would collide with a route segment or read as someone official.
_RESERVED_USERNAMES = frozenset(
    {"admin", "root", "octopus", "api", "apps", "mcp", "ws", "health", "system"}
)


class UserError(Exception):
    """A rejected account operation, with the status the route should send.

    Same shape as `TokenRotationError`: the manager decides *what* went wrong
    and how bad it is, the router only translates.
    """

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """`scrypt$n$r$p$salt$hash`, self-describing so the cost can move later."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_DKLEN,
    )
    b64 = lambda raw: base64.b64encode(raw).decode("ascii")  # noqa: E731
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time, and False rather than an exception on a malformed hash —
    a corrupt row must fail the login, not 500 the route."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(base64.b64decode(hash_b64)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, base64.b64decode(hash_b64))


# A hash of nothing in particular, used to spend the same time on a username
# that does not exist as on one that does (see `authenticate`).
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


# ---------------------------------------------------------------------------
# Bearers
# ---------------------------------------------------------------------------


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _expired(iso: str | None, *, now: datetime | None = None) -> bool:
    if not iso:
        return False
    try:
        return datetime.fromisoformat(iso) <= (now or datetime.now(UTC))
    except ValueError:
        # An unparseable expiry is treated as expired: failing closed is the
        # only safe reading of "we do not know when this stops being valid".
        return True


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def normalize_username(raw: str) -> str:
    return (raw or "").strip().lower()


def validate_username(raw: str) -> str:
    name = normalize_username(raw)
    if not _USERNAME_RE.match(name):
        raise UserError(
            "A username is 3–32 characters, starts with a letter, and uses "
            "only lowercase letters, digits, - and _"
        )
    if name in _RESERVED_USERNAMES:
        raise UserError(f"'{name}' is reserved")
    return name


def validate_password(password: str, *, username: str = "") -> None:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise UserError(
            f"A password needs at least {MIN_PASSWORD_LENGTH} characters"
        )
    if username and password.strip().lower() == username.strip().lower():
        raise UserError("A password cannot be the username")


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------


class UserManager:
    """Accounts against a `Database`. Constructed per app, like AgentManager."""

    def __init__(self, db: Any) -> None:
        self.db = db

    # -- accounts -----------------------------------------------------------

    async def _insert(
        self, *, username: str, password: str, is_admin: bool
    ) -> dict[str, Any]:
        """The row, with its data key minted at the same moment.

        A user without a DEK would be a user whose first stored credential has
        to decide what to do about it, and "create it lazily" is how two halves
        of a system end up disagreeing about whether one exists.
        """
        from . import deps

        deps.forget_accounts_exist()
        return await self.db.create_user(
            user_id=uuid.uuid4().hex[:12],
            username=username,
            password_hash=hash_password(password),
            created_at=_now(),
            is_admin=is_admin,
            dek_wrapped=wrap_dek(new_dek()),
        )

    async def create_user(
        self, *, username: str, password: str, is_admin: bool = False
    ) -> dict[str, Any]:
        name = validate_username(username)
        validate_password(password, username=name)
        if await self.db.get_user_by_username(name) is not None:
            raise UserError("That username is taken", status_code=409)
        return await self._insert(username=name, password=password, is_admin=is_admin)

    def data_key(self, user: dict[str, Any]) -> str:
        """The key this user's secrets are encrypted with.

        Unwrapped on demand rather than held: the master key is already in
        memory, so caching the plaintext DEK would only widen what a heap dump
        gives away in exchange for a PBKDF2 that is already cached.
        """
        wrapped = user.get("dek_wrapped")
        if not wrapped:
            raise UserError(
                f"user {user.get('username')!r} has no data key", status_code=500
            )
        return unwrap_dek(wrapped)

    async def data_key_for(self, user_id: str | None) -> str:
        """The key `user_id`'s secrets are encrypted with, by id.

        `None` answers `OCTOPUS_AUTH_TOKEN`, which is the pre-accounts install:
        its secrets were encrypted with it and stay that way until the upgrade
        re-keys them (multi-tenancy.md §9.3). So one call answers "what key do
        I use here" in both eras, and nothing outside this module has to know
        there are two.
        """
        from .config import settings

        if user_id is None:
            return settings.auth_token
        user = await self.db.get_user(user_id)
        if user is None:
            raise UserError("no such account", status_code=404)
        return self.data_key(user)

    async def authenticate(self, username: str, password: str) -> dict[str, Any]:
        """The user, or `UserError(401)` — never which half was wrong.

        An unknown username still pays for a hash. Without that, "no such user"
        returns in a millisecond while a wrong password takes fifty, and the
        login route becomes a way to enumerate who has an account here.
        """
        user = await self.db.get_user_by_username(normalize_username(username))
        if user is None:
            verify_password(password, _DUMMY_HASH)
            raise UserError("Wrong username or password", status_code=401)
        if not verify_password(password, user["password_hash"]):
            raise UserError("Wrong username or password", status_code=401)
        if user["disabled_at"]:
            raise UserError("This account is disabled", status_code=403)
        return user

    async def set_password(
        self, user_id: str, password: str, *, keep_token_hash: str | None = None
    ) -> int:
        """Change a password and sign the other devices out.

        Returns how many bearers were revoked. `keep_token_hash` spares the one
        making the request, so changing your password does not log you out of
        the tab you changed it in — while a stolen session elsewhere dies,
        which is usually the reason someone is changing it.
        """
        user = await self.db.get_user(user_id)
        if user is None:
            raise UserError("No such user", status_code=404)
        validate_password(password, username=user["username"])
        await self.db.update_user_field(user_id, password_hash=hash_password(password))
        return await self.db.revoke_all_tokens(
            user_id, at=_now(), keep=keep_token_hash
        )

    async def set_disabled(self, user_id: str, disabled: bool) -> None:
        user = await self.db.get_user(user_id)
        if user is None:
            raise UserError("No such user", status_code=404)
        await self.db.update_user_field(
            user_id, disabled_at=_now() if disabled else None
        )
        if disabled:
            # Disabling has to take effect now, not when the token expires.
            await self.db.revoke_all_tokens(user_id, at=_now())

    # -- bearers ------------------------------------------------------------

    async def issue_token(
        self, user_id: str, *, kind: str = "session", label: str | None = None
    ) -> str:
        """Mint a bearer and return it. This is the only moment it exists in
        readable form — the row keeps a digest."""
        if kind not in ("session", "pat"):
            raise UserError(f"Unknown token kind: {kind}")
        token = new_token()
        expires = (
            (datetime.now(UTC) + SESSION_TTL).isoformat() if kind == "session" else None
        )
        await self.db.store_token(
            token_hash=token_digest(token),
            user_id=user_id,
            kind=kind,
            created_at=_now(),
            expires_at=expires,
            label=label,
        )
        return token

    async def resolve_token(self, token: str) -> dict[str, Any] | None:
        """The user this bearer belongs to, or None.

        None covers every way a bearer can be no good — unknown, revoked,
        expired, or belonging to a disabled account — because a caller that
        distinguished them would be telling an attacker which.
        """
        if not token:
            return None
        row = await self.db.get_token(token_digest(token))
        if row is None or row["revoked_at"] or _expired(row["expires_at"]):
            return None
        user = await self.db.get_user(row["user_id"])
        if user is None or user["disabled_at"]:
            return None
        await self.db.touch_token(row["token_hash"], at=_now())
        return user

    async def revoke_token(self, token: str) -> None:
        await self.db.revoke_token(token_digest(token), at=_now())

    # -- invites ------------------------------------------------------------

    async def create_invite(
        self, *, created_by: str | None, max_uses: int = 1, ttl_days: int | None = 14
    ) -> dict[str, Any]:
        if max_uses < 1:
            raise UserError("An invite has to be good for at least one use")
        expires = (
            (datetime.now(UTC) + timedelta(days=ttl_days)).isoformat()
            if ttl_days
            else None
        )
        return await self.db.create_invite(
            code=secrets.token_urlsafe(9),
            created_by=created_by,
            created_at=_now(),
            expires_at=expires,
            max_uses=max_uses,
        )

    async def register(
        self, *, invite_code: str, username: str, password: str
    ) -> dict[str, Any]:
        """Create an account against an invite, or raise.

        The code is checked before the name is validated so a wrong code cannot
        be used to probe which usernames are taken, and it is *claimed* before
        the user is created so a failure after the claim costs a use rather
        than leaving a code that was spent on nothing.
        """
        invite = await self.db.get_invite((invite_code or "").strip())
        if invite is None or invite["revoked_at"]:
            raise UserError("That invite code is not valid", status_code=403)
        if _expired(invite["expires_at"]):
            raise UserError("That invite code has expired", status_code=403)
        if invite["used_count"] >= invite["max_uses"]:
            raise UserError("That invite code has been used", status_code=403)

        name = validate_username(username)
        validate_password(password, username=name)
        if await self.db.get_user_by_username(name) is not None:
            raise UserError("That username is taken", status_code=409)

        if not await self.db.claim_invite(invite["code"]):
            # Lost a race for the last use.
            raise UserError("That invite code has been used", status_code=403)
        return await self._insert(username=name, password=password, is_admin=False)
