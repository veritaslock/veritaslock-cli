"""Tests for the `vl service-account` command group."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import respx
from typer.testing import CliRunner

from vl.app import app
from vl.lib import store

runner = CliRunner()
IDP = "http://localhost:8080"
ORG_DTO = {"id": "org-1", "name": "globo", "displayName": "Globo", "active": True}


def _caller() -> None:
    store.ensure_local_environment_seeded()
    ident = store.add_identity("local", "USER", "u-root", "root", "root")
    store.set_user_acct(ident.id, "pw")
    store.set_default_identity("local", "root")
    now = datetime.now(timezone.utc)
    store.set_cached_token(ident.id, "caller-jwt", now, now + timedelta(hours=1))


def _created(sa_id: str = "sa-1") -> dict[str, object]:
    return {
        "id": sa_id,
        "orgId": "org-1",
        "displayName": "Ingest Bot",
        "role": "ACCOUNT",
        "status": "PENDING",
        "keyVersion": 1,
        "bootstrapHash": "boot-123",
        "publicKey": None,
    }


@respx.mock
def test_add_provisions_keypair_and_stores_locally() -> None:
    _caller()
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    create = respx.post(f"{IDP}/v1/service-accounts").mock(
        return_value=httpx.Response(201, json=_created())
    )
    patch_key = respx.patch(f"{IDP}/v1/service-accounts/sa-1/public-key").mock(
        return_value=httpx.Response(200, json={**_created(), "status": "ACTIVE"})
    )

    result = runner.invoke(
        app,
        ["service-account", "add", "Ingest Bot", "--role", "ACCOUNT", "--org", "globo"],
    )

    assert result.exit_code == 0, result.stdout
    body = json.loads(create.calls.last.request.content)
    assert body["orgId"] == "org-1"  # flat, not nested orgMembership
    assert body["role"] == "ACCOUNT"
    assert body["clientSecret"]  # generated
    assert "orgMembership" not in body

    # public key PATCH carried the bootstrapHash and a base64url key
    assert patch_key.calls.last.request.url.params["bootstrapHash"] == "boot-123"
    sent_key = json.loads(patch_key.calls.last.request.content)["publicKey"]
    assert base64.urlsafe_b64decode(sent_key + "==")  # decodes

    ident = store.get_identity("local", "ingestbot")  # slugified label
    assert ident.kind == "SERVICE_ACCOUNT"
    cred = store.get_svc_acct(ident.id)
    assert cred is not None and cred.key_version == 1
    assert Path(cred.private_key_path).read_bytes().__len__() == 64
    assert store.keys_root() / "sa-1" == Path(cred.private_key_path).parent
    assert "Client secret (shown once)" in result.stdout


@respx.mock
def test_add_retries_public_key_patch_once() -> None:
    _caller()
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    respx.post(f"{IDP}/v1/service-accounts").mock(
        return_value=httpx.Response(201, json=_created())
    )
    patch_key = respx.patch(f"{IDP}/v1/service-accounts/sa-1/public-key").mock(
        side_effect=[
            httpx.Response(503, json={"detail": "flaky", "errorCode": "INTERNAL_ERROR"}),
            httpx.Response(200, json=_created()),
        ]
    )

    result = runner.invoke(
        app,
        ["service-account", "add", "Ingest Bot", "--role", "ACCOUNT", "--org", "globo"],
    )

    assert result.exit_code == 0, result.stdout
    assert patch_key.call_count == 2


@respx.mock
def test_add_rejects_taken_label_before_any_call() -> None:
    _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.add_identity("local", "SERVICE_ACCOUNT", "sa-0", "sa-0", "ingestbot")
    create = respx.post(f"{IDP}/v1/service-accounts").mock(
        return_value=httpx.Response(201, json=_created())
    )

    result = runner.invoke(
        app,
        ["service-account", "add", "Ingest Bot", "--role", "ACCOUNT", "--org", "globo"],
    )

    assert result.exit_code == 1
    assert "already exists" in result.stdout
    assert not create.called  # bailed before touching the server


@respx.mock
def test_show_masks_secret_unless_revealed() -> None:
    _caller()
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "bot")
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.set_svc_acct(ident.id, "local", "globo", "top-secret", key_version=2)
    respx.get(f"{IDP}/v1/service-accounts/sa-1").mock(
        return_value=httpx.Response(200, json={"id": "sa-1", "displayName": "Bot", "role": "NODE", "status": "ACTIVE", "keyVersion": 2})
    )

    masked = runner.invoke(app, ["service-account", "show", "bot"], env={"VL_OUTPUT": "json"})
    assert "top-secret" not in masked.stdout and "********" in masked.stdout

    shown = runner.invoke(
        app, ["service-account", "show", "bot", "--reveal-secret"], env={"VL_OUTPUT": "json"}
    )
    assert "top-secret" in shown.stdout


@respx.mock
def test_show_rejects_non_service_account_label() -> None:
    _caller()  # 'root' is a USER identity
    result = runner.invoke(app, ["service-account", "show", "root"])
    assert result.exit_code == 1
    assert "not a service account" in result.stdout


@respx.mock
def test_list_passes_filters() -> None:
    _caller()
    route = respx.get(f"{IDP}/v1/service-accounts").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "sa-1", "displayName": "B", "role": "NODE", "status": "ACTIVE", "keyVersion": 1}]})
    )

    result = runner.invoke(
        app, ["service-account", "list", "--role", "NODE", "--include-deleted"]
    )

    assert result.exit_code == 0, result.stdout
    params = route.calls.last.request.url.params
    assert params["role"] == "NODE" and params["includeDeleted"] == "true"


@respx.mock
def test_update_partial() -> None:
    _caller()
    store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "bot")
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.set_svc_acct(store.get_identity("local", "bot").id, "local", "globo", "s")
    patch = respx.patch(f"{IDP}/v1/service-accounts/sa-1").mock(
        return_value=httpx.Response(200, json={"id": "sa-1", "displayName": "Renamed", "role": "NODE", "status": "SUSPENDED", "keyVersion": 1})
    )

    result = runner.invoke(
        app, ["service-account", "update", "bot", "--status", "SUSPENDED"]
    )

    assert result.exit_code == 0, result.stdout
    assert json.loads(patch.calls.last.request.content) == {"status": "SUSPENDED"}


def test_update_nothing_supplied_errors() -> None:
    _caller()
    store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "bot")
    result = runner.invoke(app, ["service-account", "update", "bot"])
    assert result.exit_code == 1
    assert "nothing to update" in result.stdout


@respx.mock
def test_rotate_keys_swaps_on_success() -> None:
    _caller()
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "bot")
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    key_dir = store.keys_root() / "sa-1"
    from vl.lib import keys as keylib

    priv, _ = keylib.generate_keypair(key_dir)
    original = priv.read_bytes()
    store.set_svc_acct(
        ident.id, "local", "globo", "s",
        public_key_path=str(key_dir / "public.key"),
        private_key_path=str(priv),
        key_version=1,
    )
    patch = respx.patch(f"{IDP}/v1/service-accounts/sa-1").mock(
        return_value=httpx.Response(200, json={"id": "sa-1", "keyVersion": 2})
    )

    result = runner.invoke(app, ["service-account", "rotate-keys", "bot"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(patch.calls.last.request.content)["keyVersion"] == 2
    assert priv.read_bytes() != original  # key file replaced
    assert store.get_svc_acct(ident.id).key_version == 2


@respx.mock
def test_delete_removes_local_identity_and_keys() -> None:
    _caller()
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "bot")
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    from vl.lib import keys as keylib

    key_dir = store.keys_root() / "sa-1"
    keylib.generate_keypair(key_dir)
    store.set_svc_acct(ident.id, "local", "globo", "s")
    route = respx.delete(f"{IDP}/v1/service-accounts/sa-1").mock(
        return_value=httpx.Response(204)
    )

    result = runner.invoke(app, ["service-account", "delete", "bot"])

    assert result.exit_code == 0, result.stdout
    assert route.called
    assert not key_dir.exists()
    assert store.list_identities("local", kind="SERVICE_ACCOUNT") == []


def test_get_assertion_signs_with_stored_key() -> None:
    _caller()
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "bot")
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    from vl.lib import keys as keylib

    key_dir = store.keys_root() / "sa-1"
    priv, _ = keylib.generate_keypair(key_dir)
    store.set_svc_acct(
        ident.id, "local", "globo", "s", private_key_path=str(priv), key_version=1
    )

    result = runner.invoke(app, ["service-account", "get-assertion", "bot"])

    assert result.exit_code == 0, result.stdout
    token = result.stdout.strip()
    assert token.count(".") == 2
    payload = json.loads(
        base64.urlsafe_b64decode(token.split(".")[1] + "==")
    )
    assert payload["sub"] == "sa-1"
    assert payload["aud"] == f"{IDP}/auth/service-account/authenticate"


def test_get_assertion_errors_without_key_path() -> None:
    _caller()
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "bot")
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.set_svc_acct(ident.id, "local", "globo", "s")

    result = runner.invoke(app, ["service-account", "get-assertion", "bot"])
    assert result.exit_code == 1
    assert "no private key path" in result.stdout
