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
    assert store.get_user_acct(ident.id).password_plaintext == "pw"
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


SA_DTO = {"id": "sa-1", "displayName": "sys", "role": "SYSTEM", "status": "ACTIVE", "keyVersion": 3}


@respx.mock
def test_import_service_account_validates_then_persists() -> None:
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    token = respx.post(f"{IDP}/auth/service-account/token").mock(
        return_value=httpx.Response(200, json={"accessToken": "sa-jwt", "expiresIn": 900})
    )
    respx.get(f"{IDP}/v1/service-accounts/sa-1").mock(
        return_value=httpx.Response(200, json=SA_DTO)
    )

    result = runner.invoke(
        app,
        [
            "identity", "import", "--kind", "SERVICE_ACCOUNT",
            "--client-id", "sa-1", "--secret", "shh",
            "--role", "SYSTEM", "--org", "globo", "--label", "sys",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert token.called
    ident = store.get_identity("local", "sys")
    assert ident.kind == "SERVICE_ACCOUNT"
    assert ident.server_id == "sa-1"
    cred = store.get_svc_acct(ident.id)
    assert cred is not None
    assert cred.client_secret_plaintext == "shh"
    assert cred.org_name == "globo"
    assert cred.key_version == 3  # read from the server, not assumed 1
    assert store.list_org_memberships(ident.id) == []  # no org_membership for SA


@respx.mock
def test_import_service_account_bad_secret_stores_nothing() -> None:
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    respx.post(f"{IDP}/auth/service-account/token").mock(
        return_value=httpx.Response(401, json={"detail": "Bad secret.", "errorCode": "UNAUTHORIZED"})
    )

    result = runner.invoke(
        app,
        [
            "identity", "import", "--kind", "SERVICE_ACCOUNT",
            "--client-id", "sa-1", "--secret", "wrong",
            "--role", "SYSTEM", "--org", "globo", "--label", "sys",
        ],
    )

    assert result.exit_code == 1
    assert "Bad secret" in result.stdout
    assert store.list_identities("local") == []


def test_import_service_account_missing_flags_errors() -> None:
    result = runner.invoke(
        app,
        [
            "identity", "import", "--kind", "SERVICE_ACCOUNT",
            "--role", "SYSTEM", "--org", "globo", "--label", "sys",
        ],
    )
    assert result.exit_code != 0
    assert "client-id" in result.output.lower()


def test_import_wrong_role_enum_for_kind_errors() -> None:
    result = runner.invoke(
        app,
        [
            "identity", "import", "--kind", "SERVICE_ACCOUNT",
            "--client-id", "sa-1", "--secret", "shh",
            "--role", "ORG_ADMIN", "--org", "globo", "--label", "sys",
        ],
    )
    assert result.exit_code != 0
    assert "ACCOUNT" in result.output  # lists the valid SA roles


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
    assert store.get_user_acct(ident.id).password_plaintext is None  # tier 2
    assert store.get_cached_token(ident.id) is not None

    # second call reuses the same row (no duplicate, credential untouched)
    second = runner.invoke(app, ["identity", "login", "alice"])
    assert second.exit_code == 0
    assert len(store.list_identities("local")) == 1


@respx.mock
def test_login_reuses_tier1_identity_without_touching_credential(monkeypatch) -> None:
    store.ensure_local_environment_seeded()
    existing = store.add_identity("local", "USER", "u-1", "alice", "alice-stored")
    store.set_user_acct(existing.id, "stored-pw")

    monkeypatch.setattr("typer.prompt", lambda *a, **k: "different-pw")
    respx.post(f"{IDP}/auth/user/login").mock(
        return_value=httpx.Response(200, json={"accessToken": fake_jwt("u-1"), "expiresIn": 900})
    )

    result = runner.invoke(app, ["identity", "login", "alice"])

    assert result.exit_code == 0, result.stdout
    assert store.get_user_acct(existing.id).password_plaintext == "stored-pw"
    assert len(store.list_identities("local")) == 1


def test_use_and_list_and_show(monkeypatch) -> None:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    ident = store.add_identity("local", "USER", "u-1", "alice", "alice")
    store.set_user_acct(ident.id, "sekret")
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


def test_forget_removes_identity_and_cascades_locally() -> None:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    ident = store.add_identity("local", "USER", "u-1", "alice", "alice")
    store.set_user_acct(ident.id, "sekret")
    store.upsert_org_membership(ident.id, "local", "globo", "USER")

    result = runner.invoke(app, ["identity", "forget", "alice"])

    assert result.exit_code == 0, result.stdout
    assert "server account is untouched" in result.stdout
    assert store.list_identities("local") == []
    assert store.get_user_acct(ident.id) is None
    assert store.list_org_memberships(ident.id) == []


def test_forget_unknown_label_errors() -> None:
    store.ensure_local_environment_seeded()
    result = runner.invoke(app, ["identity", "forget", "ghost"])
    assert result.exit_code == 1
    assert "No identity 'ghost'" in result.stdout
