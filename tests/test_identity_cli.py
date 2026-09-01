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
SA_DTO = {"id": "sa-1", "displayName": "sys", "role": "SYSTEM", "status": "ACTIVE", "keyVersion": 3, "orgId": "org-1"}


@respx.mock
def test_import_user_populates_memberships_from_token(monkeypatch) -> None:
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "pw")
    login = respx.post(f"{IDP}/auth/user/login").mock(
        return_value=httpx.Response(
            200,
            json={
                "accessToken": fake_jwt(
                    "u-7", orgs=[{"orgId": "org-1", "role": "ORG_ADMIN"}]
                ),
                "expiresIn": 900,
            },
        )
    )
    respx.get(f"{IDP}/v1/organizations/org-1").mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )

    result = runner.invoke(
        app, ["identity", "import", "--username", "admin", "--label", "root"]
    )

    assert result.exit_code == 0, result.stdout
    assert login.called
    ident = store.get_identity("local", "root")
    assert ident.server_id == "u-7"
    assert store.get_user_acct(ident.id).password_plaintext == "pw"
    # org + role come from the JWT `orgs` claim, not a flag
    membership = store.list_org_memberships(ident.id)[0]
    assert (membership.org_name, membership.role) == ("globo", "ORG_ADMIN")
    assert store.get_organization("local", "globo").server_org_id == "org-1"


@respx.mock
def test_import_user_with_no_orgs_claim(monkeypatch) -> None:
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "pw")
    respx.post(f"{IDP}/auth/user/login").mock(
        return_value=httpx.Response(200, json={"accessToken": fake_jwt("u-7"), "expiresIn": 900})
    )

    result = runner.invoke(
        app, ["identity", "import", "--username", "admin", "--label", "root"]
    )

    assert result.exit_code == 0, result.stdout
    assert store.list_org_memberships(store.get_identity("local", "root").id) == []


@respx.mock
def test_import_bad_credentials_stores_nothing(monkeypatch) -> None:
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "wrong")
    respx.post(f"{IDP}/auth/user/login").mock(
        return_value=httpx.Response(401, json={"detail": "Bad credentials.", "errorCode": "UNAUTHORIZED"})
    )

    result = runner.invoke(
        app, ["identity", "import", "--username", "admin", "--label", "root"]
    )

    assert result.exit_code == 1
    assert "Bad credentials" in result.stdout
    assert store.list_identities("local") == []


@respx.mock
def test_import_refuses_duplicate_account(monkeypatch) -> None:
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "pw")
    store.ensure_local_environment_seeded()
    store.add_identity("local", "USER", "u-7", "admin", "root")  # already imported
    respx.post(f"{IDP}/auth/user/login").mock(
        return_value=httpx.Response(200, json={"accessToken": fake_jwt("u-7"), "expiresIn": 900})
    )

    result = runner.invoke(
        app, ["identity", "import", "--username", "admin", "--label", "globo_admin"]
    )

    assert result.exit_code == 1
    assert "already imported as 'root'" in result.stdout
    assert [i.label for i in store.list_identities("local")] == ["root"]


@respx.mock
def test_import_service_account_validates_and_resolves_org() -> None:
    token = respx.post(f"{IDP}/auth/service-account/token").mock(
        return_value=httpx.Response(200, json={"accessToken": "sa-jwt", "expiresIn": 900})
    )
    respx.get(f"{IDP}/v1/service-accounts/sa-1").mock(
        return_value=httpx.Response(200, json=SA_DTO)
    )
    respx.get(f"{IDP}/v1/organizations/org-1").mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )

    result = runner.invoke(
        app,
        [
            "identity", "import", "--kind", "SERVICE_ACCOUNT",
            "--client-id", "sa-1", "--secret", "shh", "--label", "sys",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert token.called
    ident = store.get_identity("local", "sys")
    assert ident.kind == "SERVICE_ACCOUNT" and ident.server_id == "sa-1"
    cred = store.get_svc_acct(ident.id)
    assert cred is not None
    assert cred.client_secret_plaintext == "shh"
    assert cred.org_name == "globo"  # resolved from sa_dto.orgId, not a flag
    assert cred.key_version == 3
    assert store.list_org_memberships(ident.id) == []  # no org_membership for SA


@respx.mock
def test_import_service_account_bad_secret_stores_nothing() -> None:
    respx.post(f"{IDP}/auth/service-account/token").mock(
        return_value=httpx.Response(401, json={"detail": "Bad secret.", "errorCode": "UNAUTHORIZED"})
    )

    result = runner.invoke(
        app,
        [
            "identity", "import", "--kind", "SERVICE_ACCOUNT",
            "--client-id", "sa-1", "--secret", "wrong", "--label", "sys",
        ],
    )

    assert result.exit_code == 1
    assert "Bad secret" in result.stdout
    assert store.list_identities("local") == []


def test_import_service_account_missing_flags_errors() -> None:
    result = runner.invoke(
        app, ["identity", "import", "--kind", "SERVICE_ACCOUNT", "--label", "sys"]
    )
    assert result.exit_code != 0
    assert "client-id" in result.output.lower()


def test_import_user_missing_username_errors() -> None:
    result = runner.invoke(app, ["identity", "import", "--label", "root"])
    assert result.exit_code != 0
    assert "username" in result.output.lower()


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
