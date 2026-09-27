"""Credentials: passwords, session tokens, and CI API keys.

Two authentication paths, because there are two kinds of caller:

* **People** get a short-lived JWT after signing in. It is signed, not encrypted, and carries only
  the user id and expiry — anything else would be a claim we then have to keep in sync.
* **CI** gets an API key scoped to one project. The plaintext is shown exactly once; only its
  SHA-256 is stored, so a leaked database gives an attacker nothing to replay.

API keys are hashed with SHA-256 rather than Argon2 on purpose: they are 256 bits of entropy we
generated, so there is nothing to brute-force, and key auth happens on every CI request where a
deliberately slow hash would be a self-inflicted rate limit. Passwords are the opposite case —
they are low-entropy and human-chosen, so they get Argon2id.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

from crucible.config import settings
from crucible.errors import Unauthorized

_hasher = PasswordHasher()

API_KEY_PREFIX = "crk"
_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except VerificationError:
        # A malformed or truncated hash in the row. Treat it as a failed login rather than a 500:
        # the caller still supplied the wrong credential for this account either way.
        return False
    return True


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def create_access_token(user_id: UUID, *, ttl_seconds: int | None = None) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds or settings.access_token_ttl_seconds)).timestamp()),
        "typ": "access",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        payload: dict[str, Any] = jwt.decode(token, settings.jwt_secret, algorithms=[_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("session expired") from exc
    except jwt.InvalidTokenError as exc:
        raise Unauthorized("invalid session token") from exc
    if payload.get("typ") != "access":
        raise Unauthorized("wrong token type")
    return payload


def generate_api_key() -> tuple[str, str, str]:
    """Returns `(plaintext, prefix, sha256)`. The plaintext is never stored or logged."""
    secret = secrets.token_urlsafe(32)
    plaintext = f"{API_KEY_PREFIX}_{secret}"
    return plaintext, plaintext[:12], hash_api_key(plaintext)


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
