"""Tests for vl.lib.auth — the §7 token-acquisition flow."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

from vl.lib import api, auth, store

IDP = "http://idp.test"


def _identity(label: str = "alice", tier1_password: str | None = "pw") -> store.Identity:
    store.ensure_local_environment_seeded()
    ident = store.add_identity("local", "USER", "u-1", "alice", label)
    store.set_user_acct(ident.id, tier1_password)
    return ident


def test_uses_cached_token_without_network(isolated_store: Path) -> None:
    ident = _identity()
    now = datetime.now(timezone.utc)
    store.set_cached_token(ident.id, "cached-jwt", now, now + timedelta(hours=1))

    # No respx mock installed — a network call would raise.
    assert auth.get_token(ident, IDP) == "cached-jwt"


@respx.mock
def test_tier1_silently_reauthenticates_and_caches(isolated_store: Path) -> None:
    ident = _identity(tier1_password="pw")
    route = respx.post(f"{IDP}/auth/user/login").mock(
        return_value=httpx.Response(200, json={"accessToken": "fresh-jwt", "expiresIn": 900})
    )

    assert auth.get_token(ident, IDP) == "fresh-jwt"
    assert route.called
    assert store.get_cached_token(ident.id).token == "fresh-jwt"


@respx.mock
def test_tier2_no_tty_fails_clearly(isolated_store: Path) -> None:
    ident = _identity(tier1_password=None)  # tier 2

    with pytest.raises(auth.AuthError, match="vl identity login"):
        auth.get_token(ident, IDP, allow_prompt=False)


@respx.mock
def test_tier2_prompts_when_allowed(isolated_store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ident = _identity(tier1_password=None)
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "typed-pw")
    login = respx.post(f"{IDP}/auth/user/login").mock(
        return_value=httpx.Response(200, json={"accessToken": "jwt-2", "expiresIn": 900})
    )

    assert auth.get_token(ident, IDP, allow_prompt=True) == "jwt-2"
    assert b'"password":"typed-pw"' in login.calls.last.request.content


@respx.mock
def test_authed_call_retries_once_on_401(isolated_store: Path) -> None:
    ident = _identity(tier1_password="pw")
    now = datetime.now(timezone.utc)
    store.set_cached_token(ident.id, "stale-jwt", now, now + timedelta(hours=1))

    respx.post(f"{IDP}/auth/user/login").mock(
        return_value=httpx.Response(200, json={"accessToken": "fresh-jwt", "expiresIn": 900})
    )
    protected = respx.get(f"{IDP}/v1/users/u-1").mock(
        side_effect=[
            httpx.Response(401, json={"detail": "expired", "errorCode": "UNAUTHORIZED"}),
            httpx.Response(200, json={"id": "u-1", "username": "alice"}),
        ]
    )

    result = auth.authed_call(
        ident, IDP, lambda c: c.get("/v1/users/u-1"), allow_prompt=False
    )

    assert result["username"] == "alice"
    assert protected.call_count == 2
    assert store.get_cached_token(ident.id).token == "fresh-jwt"


@respx.mock
def test_service_account_identity_uses_symmetric_token(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True, created_at="2026-01-01T00:00:00Z")
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "sys")
    store.set_svc_acct(ident.id, "local", "globo", "sa-1", "shh", key_version=1)

    route = respx.post(f"{IDP}/auth/service-account/token").mock(
        return_value=httpx.Response(200, json={"accessToken": "sa-jwt", "expiresIn": 600})
    )

    # allow_prompt is irrelevant for this kind — no tier-2 path.
    assert auth.get_token(ident, IDP, allow_prompt=False) == "sa-jwt"
    assert route.calls.last.request.content == b'{"clientId":"sa-1","clientSecret":"shh"}'
    assert store.get_cached_token(ident.id).token == "sa-jwt"


@respx.mock
def test_service_account_identity_missing_credential_errors(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "sys")
    with pytest.raises(auth.AuthError, match="client secret"):
        auth.get_token(ident, IDP, allow_prompt=False)


@respx.mock
def test_authed_call_propagates_non_401(isolated_store: Path) -> None:
    ident = _identity(tier1_password="pw")
    now = datetime.now(timezone.utc)
    store.set_cached_token(ident.id, "jwt", now, now + timedelta(hours=1))
    respx.get(f"{IDP}/v1/users/u-1").mock(
        return_value=httpx.Response(404, json={"detail": "User not found.", "errorCode": "USER_NOT_FOUND"})
    )

    with pytest.raises(api.ApiError) as excinfo:
        auth.authed_call(ident, IDP, lambda c: c.get("/v1/users/u-1"))
    assert excinfo.value.status_code == 404
