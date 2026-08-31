"""Client-side password generation and hashing.

The IdP only ever receives a bcrypt ``passwordHash`` — plaintext never leaves the
machine (matching the old ``create_user.sh``). Cost factor 10 matches Spring's
default ``BCryptPasswordEncoder``.
"""

from __future__ import annotations

import secrets

import bcrypt

_BCRYPT_ROUNDS = 10


def generate_password(nbytes: int = 18) -> str:
    """A random URL-safe password."""
    return secrets.token_urlsafe(nbytes)


def bcrypt_hash(password: str) -> str:
    """Bcrypt hash for the server's ``passwordHash`` field."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode()
