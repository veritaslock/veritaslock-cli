"""Tests for the `vl user` command group."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import bcrypt
import httpx
import respx
from typer.testing import CliRunner

from vl.app import app
from vl.lib import store

runner = CliRunner()
IDP = "http://localhost:8080"

ORG_DTO = {"id": "org-1", "name": "globo", "displayName": "Globo", "active": True}


def _caller(label: str = "root", *, kind: str = "USER") -> store.Identity:
    """A resolved-and-tokened calling identity so commands skip re-auth."""
    store.ensure_local_environment_seeded()
    ident = store.add_identity("local", kind, "u-root", "root", label)
    if kind == "USER":
        store.set_user_acct(ident.id, "pw")
    store.set_default_identity("local", label)
    now = datetime.now(timezone.utc)
    store.set_cached_token(ident.id, "caller-jwt", now, now + timedelta(hours=1))
    return ident


@respx.mock
def test_add_creates_user_identity_credential_membership() -> None:
    _caller()
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    create = respx.post(f"{IDP}/v1/users").mock(
        return_value=httpx.Response(
            201,
            json={"id": "u-new", "username": "jdoe", "email": "john.doe@example.com", "displayName": "John Doe", "status": "ACTIVE"},
        )
    )

    result = runner.invoke(
        app, ["user", "add", "John", "Doe", "--org", "globo", "--role", "USER"]
    )

    assert result.exit_code == 0, result.stdout
    body = json.loads(create.calls.last.request.content)
    assert body["username"] == "jdoe"
    assert body["email"] == "john.doe@example.com"  # placeholder default
    assert "phoneNumber" not in body  # omitted when --phone not given
    assert body["organizations"][0] == {"orgId": "org-1", "orgName": "globo", "role": "USER"}
    # client-side bcrypt hash, never plaintext
    assert body["passwordHash"].startswith("$2")
    assert "password" not in body

    ident = store.get_identity("local", "jdoe")
    cred = store.get_user_acct(ident.id)
    assert cred is not None and cred.password_plaintext is not None
    assert bcrypt.checkpw(
        cred.password_plaintext.encode(), body["passwordHash"].encode()
    )
    assert store.list_org_memberships(ident.id)[0].role == "USER"
    assert "Password (shown once)" in result.stdout


@respx.mock
def test_add_sends_explicit_email_and_phone() -> None:
    _caller()
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    create = respx.post(f"{IDP}/v1/users").mock(
        return_value=httpx.Response(
            201,
            json={"id": "u-new", "username": "jdoe", "email": "j@real.com", "phoneNumber": "+15555550123", "displayName": "John Doe", "status": "ACTIVE"},
        )
    )

    result = runner.invoke(
        app,
        [
            "user", "add", "John", "Doe", "--org", "globo", "--role", "ORG_ADMIN",
            "--email", "j@real.com", "--phone", "+15555550123",
        ],
    )

    assert result.exit_code == 0, result.stdout
    body = json.loads(create.calls.last.request.content)
    assert body["email"] == "j@real.com"
    assert body["phoneNumber"] == "+15555550123"
    assert "+15555550123" in result.stdout


@respx.mock
def test_add_rejects_service_account_caller() -> None:
    _caller("svc", kind="SERVICE_ACCOUNT")

    result = runner.invoke(
        app, ["user", "add", "John", "Doe", "--org", "globo", "--role", "USER"]
    )
    assert result.exit_code == 1
    assert "only a USER identity" in result.stdout


def test_add_with_no_identity_errors() -> None:
    store.ensure_local_environment_seeded()  # no identities at all
    result = runner.invoke(
        app, ["user", "add", "John", "Doe", "--org", "globo", "--role", "USER"]
    )
    assert result.exit_code == 1
    assert "No identity selected" in result.stdout


@respx.mock
def test_add_surfaces_403_from_server() -> None:
    _caller()
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    respx.post(f"{IDP}/v1/users").mock(
        return_value=httpx.Response(403, json={"detail": "Not authorized to create users.", "errorCode": "ACCESS_DENIED"})
    )

    result = runner.invoke(
        app, ["user", "add", "John", "Doe", "--org", "globo", "--role", "ORG_ADMIN"]
    )
    assert result.exit_code == 1
    assert "Not authorized to create users" in result.stdout


@respx.mock
def test_show_merges_server_and_local() -> None:
    _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    target = store.add_identity("local", "USER", "u-5", "jdoe", "jdoe")
    store.set_user_acct(target.id, "local-pw")
    store.upsert_org_membership(target.id, "local", "globo", "USER")
    respx.get(f"{IDP}/v1/users/u-5").mock(
        return_value=httpx.Response(
            200,
            json={"id": "u-5", "username": "jdoe", "email": "j@example.com", "displayName": "J Doe", "status": "ACTIVE", "mfaEnabled": False},
        )
    )

    masked = runner.invoke(app, ["user", "show", "jdoe"], env={"VL_OUTPUT": "json"})
    assert masked.exit_code == 0, masked.stdout
    assert "local-pw" not in masked.stdout and "globo:USER" in masked.stdout

    revealed = runner.invoke(
        app, ["user", "show", "jdoe", "--reveal-secret"], env={"VL_OUTPUT": "json"}
    )
    assert "local-pw" in revealed.stdout


@respx.mock
def test_list_passes_filters() -> None:
    _caller()
    route = respx.get(f"{IDP}/v1/users").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "u-1", "username": "a", "email": "a@x", "status": "ACTIVE"}], "nextCursor": "1"})
    )

    result = runner.invoke(app, ["user", "list", "--status", "ACTIVE"])

    assert result.exit_code == 0, result.stdout
    assert route.calls.last.request.url.params["status"] == "ACTIVE"
    assert "--page 1" in result.stdout


@respx.mock
def test_update_partial_and_tier1_password_sync() -> None:
    _caller()
    target = store.add_identity("local", "USER", "u-5", "jdoe", "jdoe")
    store.set_user_acct(target.id, "old-pw")
    patch = respx.patch(f"{IDP}/v1/users/u-5").mock(
        return_value=httpx.Response(200, json={"id": "u-5", "username": "jdoe", "email": "j@example.com", "displayName": "New Name", "status": "ACTIVE", "mfaEnabled": False})
    )

    result = runner.invoke(
        app,
        ["user", "update", "jdoe", "--display-name", "New Name", "--password", "new-pw"],
    )

    assert result.exit_code == 0, result.stdout
    body = json.loads(patch.calls.last.request.content)
    assert body["displayName"] == "New Name"
    assert body["passwordHash"].startswith("$2")
    assert set(body) == {"displayName", "passwordHash"}  # partial
    assert store.get_user_acct(target.id).password_plaintext == "new-pw"  # tier 1 synced


@respx.mock
def test_update_tier2_password_not_stored() -> None:
    _caller()
    target = store.add_identity("local", "USER", "u-5", "jdoe", "jdoe")
    store.set_user_acct(target.id, None)  # tier 2
    respx.patch(f"{IDP}/v1/users/u-5").mock(
        return_value=httpx.Response(200, json={"id": "u-5", "username": "jdoe", "email": "j@example.com", "displayName": "J", "status": "ACTIVE", "mfaEnabled": False})
    )

    result = runner.invoke(app, ["user", "update", "jdoe", "--password", "new-pw"])

    assert result.exit_code == 0, result.stdout
    assert store.get_user_acct(target.id).password_plaintext is None


@respx.mock
def test_update_sends_phone() -> None:
    _caller()
    store.add_identity("local", "USER", "u-5", "jdoe", "jdoe")
    patch = respx.patch(f"{IDP}/v1/users/u-5").mock(
        return_value=httpx.Response(200, json={"id": "u-5", "username": "jdoe", "email": "j@x", "phoneNumber": "+15555550199", "displayName": "J", "status": "ACTIVE", "mfaEnabled": False})
    )

    result = runner.invoke(app, ["user", "update", "jdoe", "--phone", "+15555550199"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(patch.calls.last.request.content) == {"phoneNumber": "+15555550199"}


def test_update_nothing_supplied_errors() -> None:
    _caller()
    store.add_identity("local", "USER", "u-5", "jdoe", "jdoe")
    result = runner.invoke(app, ["user", "update", "jdoe"])
    assert result.exit_code == 1
    assert "nothing to update" in result.stdout


@respx.mock
def test_delete_removes_local_identity() -> None:
    _caller()
    store.add_identity("local", "USER", "u-5", "jdoe", "jdoe")
    route = respx.delete(f"{IDP}/v1/users/u-5").mock(return_value=httpx.Response(204))

    result = runner.invoke(app, ["user", "delete", "jdoe"])

    assert result.exit_code == 0, result.stdout
    assert route.called
    assert [i.label for i in store.list_identities("local", kind="USER")] == ["root"]
