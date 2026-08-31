"""Shared test helpers."""

from __future__ import annotations

import base64
import json


def fake_jwt(sub: str, **claims: object) -> str:
    """An unsigned JWT whose payload carries ``sub`` (+ any extra claims)."""

    def seg(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'EdDSA'})}.{seg({'sub': sub, **claims})}.sig"
