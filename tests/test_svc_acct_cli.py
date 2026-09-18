"""Tests for the `vl svc-acct` command group."""

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
        ["svc-acct", "add", "Ingest Bot", "--role", "ACCOUNT", "--org", "globo"],
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

    ident = store.get_identity("local", "ingest-bot")  # slugified label (hyphens preserved)
    assert ident.kind == "SERVICE_ACCOUNT"
    cred = store.get_svc_acct(ident.id)
    assert cred is not None and cred.key_version == 1
    assert cred.role == "ACCOUNT"  # cached from --role
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
        ["svc-acct", "add", "Ingest Bot", "--role", "ACCOUNT", "--org", "globo"],
    )

    assert result.exit_code == 0, result.stdout
    assert patch_key.call_count == 2


@respx.mock
def test_add_rejects_taken_label_before_any_call() -> None:
    _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.add_identity("local", "SERVICE_ACCOUNT", "sa-0", "sa-0", "ingest-bot")
    create = respx.post(f"{IDP}/v1/service-accounts").mock(
        return_value=httpx.Response(201, json=_created())
    )

    result = runner.invoke(
        app,
        ["svc-acct", "add", "Ingest Bot", "--role", "ACCOUNT", "--org", "globo"],
    )

    assert result.exit_code == 1
    assert "already exists" in result.stdout
    assert not create.called  # bailed before touching the server


@respx.mock
def test_show_masks_secret_unless_revealed() -> None:
    _caller()
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "bot")
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.set_svc_acct(ident.id, "local", "globo", "sa-1", "top-secret", key_version=2)
    respx.get(f"{IDP}/v1/service-accounts/sa-1").mock(
        return_value=httpx.Response(200, json={"id": "sa-1", "displayName": "Bot", "role": "NODE", "status": "ACTIVE", "keyVersion": 2})
    )

    masked = runner.invoke(app, ["svc-acct", "show", "bot"], env={"VL_OUTPUT": "json"})
    assert "top-secret" not in masked.stdout and "********" in masked.stdout

    shown = runner.invoke(
        app, ["svc-acct", "show", "bot", "--reveal-secret"], env={"VL_OUTPUT": "json"}
    )
    assert "top-secret" in shown.stdout


@respx.mock
def test_show_rejects_non_service_account_label() -> None:
    _caller()  # 'root' is a USER identity
    result = runner.invoke(app, ["svc-acct", "show", "root"])
    assert result.exit_code == 1
    assert "not a service account" in result.stdout


@respx.mock
def test_list_local_by_default() -> None:
    _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-9", "sa-9", "bot")
    store.set_svc_acct(ident.id, "local", "globo", "sa-9", "s", key_version=2, role="NODE")

    # No respx mock — the local listing must not hit the server.
    result = runner.invoke(app, ["svc-acct", "list"], env={"VL_OUTPUT": "json"})
    assert result.exit_code == 0, result.stdout
    rows = json.loads(result.stdout)
    assert rows[0]["org"] == "globo" and rows[0]["role"] == "NODE"


def test_list_local_role_dash_when_unknown() -> None:
    _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-old", "sa-old", "bot")
    store.set_svc_acct(ident.id, "local", "globo", "sa-old", "s")  # no role (older cache)

    result = runner.invoke(app, ["svc-acct", "list"], env={"VL_OUTPUT": "json"})
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)[0]["role"] == "-"


def test_list_local_filtered_by_org() -> None:
    _caller()
    for name in ("globo", "acme"):
        store.upsert_organization("local", name, f"o-{name}", name, active=True)
    g = store.add_identity("local", "SERVICE_ACCOUNT", "sa-g", "sa-g", "gbot")
    store.set_svc_acct(g.id, "local", "globo", "sa-g", "s", key_version=1)
    a = store.add_identity("local", "SERVICE_ACCOUNT", "sa-a", "sa-a", "abot")
    store.set_svc_acct(a.id, "local", "acme", "sa-a", "s", key_version=1)

    result = runner.invoke(
        app, ["svc-acct", "list", "--org", "acme"], env={"VL_OUTPUT": "json"}
    )
    assert result.exit_code == 0, result.stdout
    assert "abot" in result.stdout and "gbot" not in result.stdout


@respx.mock
def test_list_remote_resolves_org_to_orgid_filter() -> None:
    _caller()
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    route = respx.get(f"{IDP}/v1/service-accounts").mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    result = runner.invoke(app, ["svc-acct", "list", "--all", "--org", "globo"])

    assert result.exit_code == 0, result.stdout
    assert route.calls.last.request.url.params["orgId"] == "org-1"


@respx.mock
def test_list_remote_passes_filters() -> None:
    _caller()
    route = respx.get(f"{IDP}/v1/service-accounts").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "sa-1", "displayName": "B", "role": "NODE", "status": "ACTIVE", "keyVersion": 1}]})
    )

    result = runner.invoke(
        app, ["svc-acct", "list", "--remote", "--role", "NODE", "--include-deleted"]
    )

    assert result.exit_code == 0, result.stdout
    params = route.calls.last.request.url.params
    assert params["role"] == "NODE" and params["includeDeleted"] == "true"


@respx.mock
def test_list_remote_shows_org_resolved_from_orgid() -> None:
    _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)  # cached
    respx.get(f"{IDP}/v1/service-accounts").mock(
        return_value=httpx.Response(200, json={"items": [
            {"id": "sa-1", "displayName": "B", "role": "NODE", "status": "ACTIVE", "keyVersion": 1, "orgId": "org-1"},
            {"id": "sa-2", "displayName": "C", "role": "NODE", "status": "ACTIVE", "keyVersion": 1, "orgId": "org-2"},
        ]})
    )
    respx.get(f"{IDP}/v1/organizations/org-2").mock(
        return_value=httpx.Response(200, json={"id": "org-2", "name": "acme", "displayName": "Acme", "active": True})
    )

    result = runner.invoke(app, ["svc-acct", "list", "--remote"], env={"VL_OUTPUT": "json"})

    assert result.exit_code == 0, result.stdout
    rows = json.loads(result.stdout)
    assert {r["id"]: r["org"] for r in rows} == {"sa-1": "globo", "sa-2": "acme"}


@respx.mock
def test_list_remote_backfills_cached_role_and_key_version() -> None:
    _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "bot")
    store.set_svc_acct(ident.id, "local", "globo", "sa-1", "s")  # role/key_version unknown
    respx.get(f"{IDP}/v1/service-accounts").mock(
        return_value=httpx.Response(200, json={"items": [
            {"id": "sa-1", "displayName": "B", "role": "INGEST_CLIENT", "status": "ACTIVE", "keyVersion": 4, "orgId": "org-1"},
            {"id": "sa-x", "displayName": "X", "role": "SYSTEM", "status": "ACTIVE", "keyVersion": 9, "orgId": "org-1"},
        ]})
    )

    result = runner.invoke(app, ["svc-acct", "list", "--all"], env={"VL_OUTPUT": "json"})

    assert result.exit_code == 0, result.stdout
    cred = store.get_svc_acct(ident.id)
    assert cred.role == "INGEST_CLIENT" and cred.key_version == 4  # healed from server
    # the uncached sa-x got no local row
    assert store.get_identity_by_server_id("local", "sa-x", "SERVICE_ACCOUNT") is None
    # `cached` column marks which server rows exist locally
    assert {r["id"]: r["cached"] for r in json.loads(result.stdout)} == {
        "sa-1": "yes",
        "sa-x": "no",
    }


@respx.mock
def test_show_all_backfills_cached_role() -> None:
    _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "bot")
    store.set_svc_acct(ident.id, "local", "globo", "sa-1", "s", key_version=1)
    respx.get(f"{IDP}/v1/service-accounts/sa-1").mock(
        return_value=httpx.Response(200, json={
            "id": "sa-1", "displayName": "B", "role": "INGEST_CLIENT",
            "status": "ACTIVE", "keyVersion": 2, "orgId": "org-1",
        })
    )

    result = runner.invoke(app, ["svc-acct", "show", "bot", "--all"])

    assert result.exit_code == 0, result.stdout
    cred = store.get_svc_acct(ident.id)
    assert cred.role == "INGEST_CLIENT" and cred.key_version == 2


@respx.mock
def test_update_partial() -> None:
    _caller()
    store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "bot")
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.set_svc_acct(store.get_identity("local", "bot").id, "local", "globo", "sa-1", "s")
    patch = respx.patch(f"{IDP}/v1/service-accounts/sa-1").mock(
        return_value=httpx.Response(200, json={"id": "sa-1", "displayName": "Renamed", "role": "NODE", "status": "SUSPENDED", "keyVersion": 1})
    )

    result = runner.invoke(
        app, ["svc-acct", "update", "bot", "--status", "SUSPENDED"]
    )

    assert result.exit_code == 0, result.stdout
    assert json.loads(patch.calls.last.request.content) == {"status": "SUSPENDED"}


@respx.mock
def test_update_role_syncs_local_cache() -> None:
    _caller()
    store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "bot")
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    ident = store.get_identity("local", "bot").id
    store.set_svc_acct(ident, "local", "globo", "sa-1", "s", role="ACCOUNT")
    respx.patch(f"{IDP}/v1/service-accounts/sa-1").mock(
        return_value=httpx.Response(200, json={"id": "sa-1", "displayName": "b", "role": "NODE", "status": "ACTIVE", "keyVersion": 1})
    )

    result = runner.invoke(app, ["svc-acct", "update", "bot", "--role", "NODE"])

    assert result.exit_code == 0, result.stdout
    assert store.get_svc_acct(ident).role == "NODE"


def test_update_nothing_supplied_errors() -> None:
    _caller()
    store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "bot")
    result = runner.invoke(app, ["svc-acct", "update", "bot"])
    assert result.exit_code == 1
    assert "nothing to update" in result.stdout


def test_rotate_keys_command_is_absent() -> None:
    # Removed until the IdP supports re-keying an active service account.
    # See docs/features/cli-implementation/svc-acct-key-rotation-gap.md.
    from vl.commands import svc_acct

    names = {c.name for c in svc_acct.app.registered_commands}
    assert "rotate-keys" not in names

    result = runner.invoke(app, ["svc-acct", "rotate-keys", "bot"])
    assert result.exit_code == 0  # unknown subcommand -> prints `svc-acct` help
    assert "rotate-keys" not in result.output
    assert "get-assertion" in result.output  # the real command list


@respx.mock
def test_delete_removes_local_identity_and_keys() -> None:
    _caller()
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "bot")
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    from vl.lib import keys as keylib

    key_dir = store.keys_root() / "sa-1"
    keylib.generate_keypair(key_dir)
    store.set_svc_acct(ident.id, "local", "globo", "sa-1", "s")
    route = respx.delete(f"{IDP}/v1/service-accounts/sa-1").mock(
        return_value=httpx.Response(204)
    )

    result = runner.invoke(app, ["svc-acct", "delete", "bot"])

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
        ident.id, "local", "globo", "sa-1", "s", private_key_path=str(priv), key_version=1
    )

    result = runner.invoke(app, ["svc-acct", "get-assertion", "bot"])

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
    store.set_svc_acct(ident.id, "local", "globo", "sa-1", "s")

    result = runner.invoke(app, ["svc-acct", "get-assertion", "bot"])
    assert result.exit_code == 1
    assert "no private key path" in result.stdout


# --------------------------------------------------------------------------- #
# credential management (folded in from `vl identity`)
# --------------------------------------------------------------------------- #

_SA_DTO = {"id": "sa-1", "displayName": "sys", "role": "SYSTEM", "status": "ACTIVE", "keyVersion": 3, "orgId": "org-1"}


@respx.mock
def test_cache_validates_and_resolves_org() -> None:
    token = respx.post(f"{IDP}/auth/service-account/token").mock(
        return_value=httpx.Response(200, json={"accessToken": "sa-jwt", "expiresIn": 900})
    )
    respx.get(f"{IDP}/v1/service-accounts/sa-1").mock(
        return_value=httpx.Response(200, json=_SA_DTO)
    )
    respx.get(f"{IDP}/v1/organizations/org-1").mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )

    result = runner.invoke(
        app,
        ["svc-acct", "cache", "--client-id", "sa-1", "--secret", "shh", "--label", "sys"],
    )

    assert result.exit_code == 0, result.stdout
    assert token.called
    ident = store.get_identity("local", "sys")
    assert ident.kind == "SERVICE_ACCOUNT" and ident.server_id == "sa-1"
    cred = store.get_svc_acct(ident.id)
    assert cred.client_secret_plaintext == "shh"
    assert cred.org_name == "globo"  # from sa_dto.orgId, not a flag
    assert cred.key_version == 3
    assert cred.role == "SYSTEM"  # cached from the server record


@respx.mock
def test_cache_label_defaults_to_slugified_display_name() -> None:
    respx.post(f"{IDP}/auth/service-account/token").mock(
        return_value=httpx.Response(200, json={"accessToken": "sa-jwt", "expiresIn": 900})
    )
    respx.get(f"{IDP}/v1/service-accounts/sa-1").mock(
        return_value=httpx.Response(200, json={**_SA_DTO, "displayName": "Ingest Bot"})
    )
    respx.get(f"{IDP}/v1/organizations/org-1").mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )

    result = runner.invoke(
        app, ["svc-acct", "cache", "--client-id", "sa-1", "--secret", "shh"]
    )

    assert result.exit_code == 0, result.stdout
    assert store.get_identity("local", "ingest-bot").server_id == "sa-1"


@respx.mock
def test_cache_imports_private_key_into_managed_dir(tmp_path: Path) -> None:
    respx.post(f"{IDP}/auth/service-account/token").mock(
        return_value=httpx.Response(200, json={"accessToken": "sa-jwt", "expiresIn": 900})
    )
    respx.get(f"{IDP}/v1/service-accounts/sa-1").mock(
        return_value=httpx.Response(200, json=_SA_DTO)
    )
    respx.get(f"{IDP}/v1/organizations/org-1").mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    external = tmp_path / "provisioned"
    from vl.lib import keys

    src_priv, _ = keys.generate_keypair(external)

    result = runner.invoke(
        app,
        [
            "svc-acct", "cache", "--client-id", "sa-1", "--secret", "shh",
            "--label", "sys", "--private-key-path", str(src_priv),
        ],
    )

    assert result.exit_code == 0, result.stdout
    ident = store.get_identity("local", "sys")
    cred = store.get_svc_acct(ident.id)
    managed = store.keys_root() / "sa-1"
    assert Path(cred.private_key_path) == managed / "private.key"
    assert Path(cred.public_key_path) == managed / "public.key"
    assert Path(cred.private_key_path).read_bytes() == src_priv.read_bytes()
    assert (managed / "public.key").read_bytes() == (external / "public.key").read_bytes()


@respx.mock
def test_cache_bad_secret_stores_nothing() -> None:
    respx.post(f"{IDP}/auth/service-account/token").mock(
        return_value=httpx.Response(401, json={"detail": "Bad secret.", "errorCode": "UNAUTHORIZED"})
    )
    result = runner.invoke(
        app,
        ["svc-acct", "cache", "--client-id", "sa-1", "--secret", "no", "--label", "sys"],
    )
    assert result.exit_code == 1
    assert "Bad secret" in result.stdout
    assert store.list_identities("local") == []


@respx.mock
def test_cache_refuses_duplicate() -> None:
    store.ensure_local_environment_seeded()
    store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "sys")
    respx.post(f"{IDP}/auth/service-account/token").mock(
        return_value=httpx.Response(200, json={"accessToken": "sa-jwt", "expiresIn": 900})
    )
    result = runner.invoke(
        app,
        ["svc-acct", "cache", "--client-id", "sa-1", "--secret", "shh", "--label", "other"],
    )
    assert result.exit_code == 1
    assert "already cached as 'sys'" in result.stdout


def test_use_and_clear_removes_keys() -> None:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "sys")
    store.set_svc_acct(ident.id, "local", "globo", "sa-1", "s")
    from vl.lib import keys as keylib

    key_dir = store.keys_root() / "sa-1"
    keylib.generate_keypair(key_dir)

    assert runner.invoke(app, ["svc-acct", "use", "sys"]).exit_code == 0
    assert store.resolve_identity("local", None).label == "sys"

    result = runner.invoke(app, ["svc-acct", "clear", "sys"])
    assert result.exit_code == 0
    assert "server account is untouched" in result.stdout
    assert store.list_identities("local", kind="SERVICE_ACCOUNT") == []
    assert not key_dir.exists()


@respx.mock
def test_add_defaults_org_to_callers_org() -> None:
    _caller()
    caller_ident = store.get_identity("local", "root")
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.upsert_org_membership(caller_ident.id, "local", "globo", "ORG_ADMIN")
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    create = respx.post(f"{IDP}/v1/service-accounts").mock(
        return_value=httpx.Response(201, json=_created())
    )
    respx.patch(f"{IDP}/v1/service-accounts/sa-1/public-key").mock(
        return_value=httpx.Response(200, json=_created())
    )

    result = runner.invoke(app, ["svc-acct", "add", "Bot", "--role", "ACCOUNT"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(create.calls.last.request.content)["orgId"] == "org-1"
