"""Symmetric encryption for at-rest secrets, and the key hierarchy above it.

`encrypt`/`decrypt` are unchanged: Fernet with a key derived from a passphrase
by PBKDF2. What changed is *which* passphrase, and why (multi-tenancy.md §4).

The single-user build derived that key from `OCTOPUS_AUTH_TOKEN` — the same
string the client sent as its credential. That cannot survive accounts, and not
for the reason it looks like. The reason is a schedule:

> A schedule fires at 07:00 while its owner is asleep and not signed in. The
> server has to decrypt that user's Claude credential to run the turn. A key
> derived from their password does not exist at that moment.

So the password authenticates and nothing else, and the hierarchy is:

    master key                    server-held: a file on this box, or handed in
      └── wraps each user's DEK      through the environment from a secret manager
            └── encrypts that user's credentials and connector tokens

Two consequences worth stating rather than discovering. Changing a password
re-encrypts nothing, because it never was the key. And deleting a user is a
crypto-shred — drop the DEK and their ciphertext is noise, whatever is still on
disk.

The threat model is unchanged and still modest: this defends the database file
at rest, not a server that is already compromised. Under §2's isolation model
the server can read every user's secrets by design, because it runs their CLIs
while they are offline.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import logging
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# Static salt — fine here because the input (OCTOPUS_AUTH_TOKEN) already
# has user-controlled entropy. PBKDF2 only protects against weak tokens.
_SALT = b"octopus-credentials-v1"
_ITERATIONS = 200_000


@functools.lru_cache(maxsize=4)
def _key_from_secret(secret: str) -> bytes:
    """Derive a 32-byte Fernet key from the auth token."""
    raw = hashlib.pbkdf2_hmac(
        "sha256", secret.encode("utf-8"), _SALT, _ITERATIONS, dklen=32
    )
    return base64.urlsafe_b64encode(raw)


def encrypt(plaintext: str, secret: str) -> str:
    """Encrypt `plaintext` with a key derived from `secret`. Returns ASCII."""
    f = Fernet(_key_from_secret(secret))
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str, secret: str) -> str:
    """Inverse of encrypt(). Raises ValueError on bad ciphertext or wrong key."""
    f = Fernet(_key_from_secret(secret))
    try:
        return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("could not decrypt — wrong key or corrupted data") from e


# ---------------------------------------------------------------------------
# The key hierarchy
# ---------------------------------------------------------------------------

_MASTER_CACHE: dict[str, str] = {}


def master_key() -> str:
    """The server's key-wrapping key, created on first use.

    `OCTOPUS_MASTER_KEY` wins when set — that is the hook a secret manager
    hangs on, and the cloud version will use nothing else. Otherwise it lives in
    a 0600 file beside the rest of Octopus's state, generated the first time it
    is wanted.

    Generating rather than demanding is deliberate: a key an operator has to
    invent and remember is a key that gets set to something guessable, pasted
    into a shell history, or lost — and losing it means every stored credential
    is gone. A file created once, with the right mode, is the version of this
    that survives contact with a real machine.
    """
    from .config import settings

    if settings.master_key:
        return settings.master_key

    path = Path(settings.master_key_file).expanduser()
    cached = _MASTER_CACHE.get(str(path))
    if cached:
        return cached

    if path.exists():
        key = path.read_text().strip()
        if not key:
            raise ValueError(
                f"{path} is empty — refusing to invent a new master key, because "
                "doing so would silently orphan every stored secret. Restore the "
                "file or set OCTOPUS_MASTER_KEY."
            )
    else:
        key = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written 0600 from the start rather than chmod'ed after: between the
        # two there is a window where the key is world-readable.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key.encode("utf-8"))
        finally:
            os.close(fd)
        logger.info("created a new master key at %s", path)

    _MASTER_CACHE[str(path)] = key
    return key


def new_dek() -> str:
    """A fresh per-user data key."""
    return secrets.token_urlsafe(32)


def wrap_dek(dek: str, *, key: str | None = None) -> str:
    """A user's DEK, encrypted under the master key, for storage in their row."""
    return encrypt(dek, key or master_key())


def unwrap_dek(wrapped: str, *, key: str | None = None) -> str:
    return decrypt(wrapped, key or master_key())
