"""Tests for the `vl identity` command group."""

from __future__ import annotations

import httpx
import respx
from typer.testing import CliRunner

from vl.app import app
from vl.lib import store

from _helpers import fake_jwt

runner = CliRunner()
IDP = "http://localhost:8080"

ORG_DTO = {"id": "org-1", "name": "globo", "displayName": "Globo", "active": True}


@respx.mock
def test_import_validates_then_persists_tier1(monkeypatch) -> None:
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "pw")
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    login = respx.post(f"{IDP}/auth/user/login").mock(
        return_value=httpx.Response(200, json={"accessToken": fake_jwt("u-7"), "expiresIn": 900})
    )

    result = runner.invoke(
        app,
        [
            "identity", "import",
            "--username", "admin", "--org", "globo",
            "--role", "ORG_ADMIN", "--label", "root",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert login.called
    ident = store.get_identity("local", "root")
    assert ident.server_id == "u-7"
    assert store.get_user_credential(ident.id).password_plaintext == "pw"
    assert store.list_org_memberships(ident.id)[0].role == "ORG_ADMIN"
    assert store.get_organization("local", "globo").server_org_id == "org-1"


@respx.mock
def test_import_bad_credentials_stores_nothing(monkeypatch) -> None:
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "wrong")
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    respx.post(f"{IDP}/auth/user/login").mock(
        return_value=httpx.Response(401, json={"detail": "Bad credentials.", "errorCode": "UNAUTHORIZED"})
    )

    result = runner.invoke(
        app,
        [
            "identity", "import",
            "--username", "admin", "--org", "globo",
            "--role", "USER", "--label", "root",
        ],
    )

    assert result.exit_code == 1
    assert "Bad credentials" in result.stdout
    assert store.list_identities("local") == []


@respx.mock
def test_import_rejects_service_account_kind() -> None:
    result = runner.invoke(
        app,
        [
            "identity", "import", "--kind", "SERVICE_ACCOUNT",
            "--username", "svc", "--org", "globo",
            "--role", "USER", "--label", "svc",
        ],
    )
    assert result.exit_code == 1
    assert "Phase 4" in result.stdout


@respx.mock
def test_login_creates_tier2_then_reuses(monkeypatch) -> None:
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "my-pw")
    respx.post(f"{IDP}/auth/user/login").mock(
        return_value=httpx.Response(200, json={"accessToken": fake_jwt("u-9"), "expiresIn": 900})
    )

    first = runner.invoke(app, ["identity", "login", "alice"])
    assert first.exit_code == 0, first.stdout

    ident = store.get_identity_by_principal("local", "alice", "USER")
    assert ident is not None
    assert store.get_user_credential(ident.id).password_plaintext is None  # tier 2
    assert store.get_cached_token(ident.id) is not None

    # second call reuses the same row (no duplicate, credential untouched)
    second = runner.invoke(app, ["identity", "login", "alice"])
    assert second.exit_code == 0
    assert len(store.list_identities("local")) == 1


@respx.mock
def test_login_reuses_tier1_identity_without_touching_credential(monkeypatch) -> None:
    store.ensure_local_environment_seeded()
    existing = store.add_identity("local", "USER", "u-1", "alice", "alice-stored")
    store.set_user_credential(existing.id, "stored-pw")

    monkeypatch.setattr("typer.prompt", lambda *a, **k: "different-pw")
    respx.post(f"{IDP}/auth/user/login").mock(
        return_value=httpx.Response(200, json={"accessToken": fake_jwt("u-1"), "expiresIn": 900})
    )

    result = runner.invoke(app, ["identity", "login", "alice"])

    assert result.exit_code == 0, result.stdout
    assert store.get_user_credential(existing.id).password_plaintext == "stored-pw"
    assert len(store.list_identities("local")) == 1


def test_use_and_list_and_show(monkeypatch) -> None:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    ident = store.add_identity("local", "USER", "u-1", "alice", "alice")
    store.set_user_credential(ident.id, "sekret")
    store.upsert_org_membership(ident.id, "local", "globo", "USER")

    assert runner.invoke(app, ["identity", "use", "alice"]).exit_code == 0

    listed = runner.invoke(app, ["identity", "list"], env={"VL_OUTPUT": "json"})
    assert "alice" in listed.stdout and "globo:USER" in listed.stdout

    masked = runner.invoke(app, ["identity", "show", "alice"], env={"VL_OUTPUT": "json"})
    assert "sekret" not in masked.stdout and "********" in masked.stdout

    revealed = runner.invoke(
        app, ["identity", "show", "alice", "--reveal-secret"], env={"VL_OUTPUT": "json"}
    )
    assert "sekret" in revealed.stdout
