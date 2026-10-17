"""Shared test helpers."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

from vl.lib import store


def fake_jwt(sub: str, **claims: object) -> str:
    """An unsigned JWT whose payload carries ``sub`` (+ any extra claims)."""

    def seg(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'EdDSA'})}.{seg({'sub': sub, **claims})}.sig"


def add_user(
    label: str = "ops",
    orgs: list[dict[str, str]] | None = None,
    *,
    typ: str | None = "user",
    default: bool = True,
    token: str | None = None,
) -> store.Identity:
    """A stored USER identity whose cached token is decodable and carries ``orgs``.

    ``orgs`` is the token's claim, e.g. ``[{"orgId": "org-1", "role": "ORG_ADMIN"}]``.
    ``typ=None`` leaves the claim out; ``token`` overrides the token entirely.
    """
    store.ensure_local_environment_seeded()
    ident = store.add_identity("local", "USER", f"u-{label}", label, label)
    store.set_user_acct(ident.id, "pw")
    if default:
        store.set_default_identity("local", label)
    claims: dict[str, object] = {"orgs": orgs or []}
    if typ is not None:
        claims["typ"] = typ
    now = datetime.now(timezone.utc)
    store.set_cached_token(
        ident.id, token or fake_jwt(f"u-{label}", **claims), now, now + timedelta(hours=1)
    )
    return ident


def add_service_account(label: str, org: str, role: str = "NODE") -> store.Identity:
    """A stored SERVICE_ACCOUNT identity with a cached service-account token."""
    store.ensure_local_environment_seeded()
    ident = store.add_identity("local", "SERVICE_ACCOUNT", f"sa-{label}", label, label)
    store.set_svc_acct(ident.id, "local", org, f"client-{label}", "secret", role=role)
    now = datetime.now(timezone.utc)
    store.set_cached_token(
        ident.id, fake_jwt(f"sa-{label}", typ="service-account", role=role, org="org-1"),
        now, now + timedelta(hours=1),
    )
    return ident
