"""Symmetric encryption for at-rest secrets (backend credentials).

Uses Fernet with a key derived from `OCTOPUS_AUTH_TOKEN` via PBKDF2.
This is intentionally lightweight — the threat model is "another local
user reading the SQLite file", not a determined attacker. Anyone with
the auth token can decrypt regardless of mechanism.
"""

from __future__ import annotations

import base64
import functools
import hashlib

from cryptography.fernet import Fernet, InvalidToken

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
