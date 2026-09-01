"""Tests for the `vl team` command group."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import respx
from typer.testing import CliRunner

from vl.app import app
from vl.lib import store

runner = CliRunner()
IDP = "http://localhost:8080"
ORG_DTO = {"id": "org-1", "name": "globo", "displayName": "Globo", "active": True}
TEAM_DTO = {
    "id": "t-1",
    "name": "ingest",
    "description": "Ingest team",
    "orgId": "org-1",
    "createdBy": "u-root",
    "createdAt": "2026-09-01T00:00:00Z",
}


def _caller() -> store.Identity:
    store.ensure_local_environment_seeded()
    ident = store.add_identity("local", "USER", "u-root", "root", "root")
    store.set_user_acct(ident.id, "pw")
    store.set_default_identity("local", "root")
    now = datetime.now(timezone.utc)
    store.set_cached_token(ident.id, "caller-jwt", now, now + timedelta(hours=1))
    return ident


def _mock_org() -> None:
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )


@respx.mock
def test_add_creates_team_and_caches_creator_membership() -> None:
    caller = _caller()
    _mock_org()
    create = respx.post(f"{IDP}/v1/teams").mock(
        return_value=httpx.Response(201, json=TEAM_DTO)
    )

    result = runner.invoke(
        app, ["team", "add", "globo", "ingest", "--description", "Ingest team"]
    )

    assert result.exit_code == 0, result.stdout
    body = json.loads(create.calls.last.request.content)
    assert body == {"orgId": "org-1", "name": "ingest", "description": "Ingest team"}
    team = store.get_team("local", "globo", "ingest")
    assert team.server_team_id == "t-1"
    assert store.list_team_memberships(caller.id)[0].role == "TEAM_ADMIN"


@respx.mock
def test_add_surfaces_service_account_rejection() -> None:
    # No caller-kind pre-check: vl sends the request and surfaces the server's 403.
    store.ensure_local_environment_seeded()
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "sys")
    store.set_default_identity("local", "sys")
    now = datetime.now(timezone.utc)
    store.set_cached_token(ident.id, "sa-jwt", now, now + timedelta(hours=1))
    _mock_org()
    respx.post(f"{IDP}/v1/teams").mock(
        return_value=httpx.Response(403, json={"detail": "Team creation requires a user token.", "errorCode": "ACCESS_DENIED"})
    )

    result = runner.invoke(app, ["team", "add", "globo", "ingest"])
    assert result.exit_code == 1
    assert "requires a user token" in result.stdout


@respx.mock
def test_show_resolves_by_name() -> None:
    _caller()
    _mock_org()
    respx.get(f"{IDP}/v1/teams", params={"orgId": "org-1", "name": "ingest"}).mock(
        return_value=httpx.Response(200, json={"items": [TEAM_DTO], "nextCursor": None})
    )

    result = runner.invoke(app, ["team", "show", "globo", "ingest"], env={"VL_OUTPUT": "json"})

    assert result.exit_code == 0, result.stdout
    assert "t-1" in result.stdout
    assert store.get_team("local", "globo", "ingest").description == "Ingest team"


@respx.mock
def test_show_unknown_team_errors() -> None:
    _caller()
    _mock_org()
    respx.get(f"{IDP}/v1/teams", params={"orgId": "org-1", "name": "nope"}).mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    result = runner.invoke(app, ["team", "show", "globo", "nope"])
    assert result.exit_code == 1
    assert "No team 'nope'" in result.stdout


@respx.mock
def test_list_caches_each_team() -> None:
    _caller()
    _mock_org()
    respx.get(f"{IDP}/v1/teams", params={"orgId": "org-1"}).mock(
        return_value=httpx.Response(200, json={"items": [TEAM_DTO], "nextCursor": None})
    )
    result = runner.invoke(app, ["team", "list", "globo"])
    assert result.exit_code == 0, result.stdout
    assert store.list_teams("local", "globo")[0].name == "ingest"


@respx.mock
def test_update_uses_cached_team_id() -> None:
    _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.upsert_team("local", "globo", "ingest", "t-1")
    _mock_org()
    patch = respx.patch(f"{IDP}/v1/teams/t-1").mock(
        return_value=httpx.Response(200, json={**TEAM_DTO, "description": "new"})
    )

    result = runner.invoke(
        app, ["team", "update", "globo", "ingest", "--description", "new"]
    )

    assert result.exit_code == 0, result.stdout
    assert not respx.calls[-1].request.url.path.endswith("/v1/teams")  # no name lookup
    assert json.loads(patch.calls.last.request.content) == {"description": "new"}
    assert store.get_team("local", "globo", "ingest").description == "new"


@respx.mock
def test_update_requires_a_field() -> None:
    _caller()
    result = runner.invoke(app, ["team", "update", "globo", "ingest"])
    assert result.exit_code == 1
    assert "nothing to update" in result.stdout


@respx.mock
def test_update_rename_recaches_and_drops_old_row() -> None:
    _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.upsert_team("local", "globo", "ingest", "t-1")
    _mock_org()
    respx.patch(f"{IDP}/v1/teams/t-1").mock(
        return_value=httpx.Response(200, json={**TEAM_DTO, "name": "ingestion"})
    )

    result = runner.invoke(
        app, ["team", "update", "globo", "ingest", "--name", "ingestion"]
    )

    assert result.exit_code == 0, result.stdout
    assert store.get_team_or_none("local", "globo", "ingest") is None
    assert store.get_team("local", "globo", "ingestion").server_team_id == "t-1"


@respx.mock
def test_delete_resolves_then_drops_local() -> None:
    _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.upsert_team("local", "globo", "ingest", "t-1")
    _mock_org()
    route = respx.delete(f"{IDP}/v1/teams/t-1").mock(return_value=httpx.Response(204))

    result = runner.invoke(app, ["team", "delete", "globo", "ingest"])

    assert result.exit_code == 0, result.stdout
    assert route.called
    assert store.get_team_or_none("local", "globo", "ingest") is None


@respx.mock
def test_members_list_refreshes_cache_for_known_identities() -> None:
    _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.upsert_team("local", "globo", "ingest", "t-1")
    known = store.add_identity("local", "USER", "u-9", "jdoe", "jdoe")
    _mock_org()
    respx.get(f"{IDP}/v1/teams/t-1/members").mock(
        return_value=httpx.Response(
            200,
            json={"items": [
                {"teamId": "t-1", "userId": "u-9", "role": "TEAM_ADMIN", "addedAt": "x"},
                {"teamId": "t-1", "userId": "u-unknown", "role": "TEAM_MEMBER", "addedAt": "x"},
            ]},
        )
    )

    result = runner.invoke(app, ["team", "members", "list", "globo", "ingest"])

    assert result.exit_code == 0, result.stdout
    memberships = store.list_team_memberships(known.id)
    assert [(m.team_name, m.role) for m in memberships] == [("ingest", "TEAM_ADMIN")]


@respx.mock
def test_members_add_defaults_role_and_upserts_known() -> None:
    _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.upsert_team("local", "globo", "ingest", "t-1")
    known = store.add_identity("local", "USER", "u-9", "jdoe", "jdoe")
    _mock_org()
    add = respx.post(f"{IDP}/v1/teams/t-1/members").mock(
        return_value=httpx.Response(201, json={"teamId": "t-1", "userId": "u-9", "role": "TEAM_MEMBER"})
    )

    result = runner.invoke(
        app, ["team", "members", "add", "globo", "ingest", "--user-id", "u-9"]
    )

    assert result.exit_code == 0, result.stdout
    assert json.loads(add.calls.last.request.content) == {"userId": "u-9"}  # role omitted
    assert store.list_team_memberships(known.id)[0].role == "TEAM_MEMBER"


@respx.mock
def test_members_set_role_and_remove() -> None:
    _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.upsert_team("local", "globo", "ingest", "t-1")
    known = store.add_identity("local", "USER", "u-9", "jdoe", "jdoe")
    store.upsert_team_member(known.id, "local", "globo", "ingest", "TEAM_MEMBER")
    _mock_org()
    respx.patch(f"{IDP}/v1/teams/t-1/members/u-9").mock(
        return_value=httpx.Response(200, json={"teamId": "t-1", "userId": "u-9", "role": "TEAM_ADMIN"})
    )
    respx.delete(f"{IDP}/v1/teams/t-1/members/u-9").mock(return_value=httpx.Response(204))

    set_role = runner.invoke(
        app, ["team", "members", "set-role", "globo", "ingest", "u-9", "--role", "TEAM_ADMIN"]
    )
    assert set_role.exit_code == 0, set_role.stdout
    assert store.list_team_memberships(known.id)[0].role == "TEAM_ADMIN"

    remove = runner.invoke(app, ["team", "members", "remove", "globo", "ingest", "u-9"])
    assert remove.exit_code == 0, remove.stdout
    assert store.list_team_memberships(known.id) == []


@respx.mock
def test_ingest_client_add_creates_service_account_identity() -> None:
    _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.upsert_team("local", "globo", "ingest", "t-1")
    _mock_org()
    respx.post(f"{IDP}/v1/teams/t-1/ingest-clients").mock(
        return_value=httpx.Response(
            201,
            json={"id": "sa-9", "displayName": "edge-01", "clientSecret": "sek", "keyVersion": 1, "status": "ACTIVE", "createdAt": "x"},
        )
    )

    result = runner.invoke(
        app, ["team", "ingest-clients", "add", "globo", "ingest", "edge-01"]
    )

    assert result.exit_code == 0, result.stdout
    assert "sek" in result.stdout  # secret shown once
    ident = store.get_identity("local", "ingest-edge-01")
    assert ident.kind == "SERVICE_ACCOUNT" and ident.server_id == "sa-9"
    cred = store.get_svc_acct(ident.id)
    assert cred is not None
    assert cred.client_secret_plaintext == "sek"
    assert cred.private_key_path is None  # ingest clients never get a keypair


@respx.mock
def test_ingest_client_rotate_and_delete_track_local() -> None:
    _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.upsert_team("local", "globo", "ingest", "t-1")
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-9", "sa-9", "ingest-edge-01")
    store.set_svc_acct(ident.id, "local", "globo", "old", key_version=1)
    _mock_org()
    respx.post(f"{IDP}/v1/teams/t-1/ingest-clients/sa-9/rotate").mock(
        return_value=httpx.Response(200, json={"id": "sa-9", "displayName": "edge-01", "clientSecret": "new", "keyVersion": 2, "status": "ACTIVE", "createdAt": "x"})
    )
    respx.delete(f"{IDP}/v1/teams/t-1/ingest-clients/sa-9").mock(return_value=httpx.Response(204))

    rotate = runner.invoke(
        app, ["team", "ingest-clients", "rotate", "globo", "ingest", "sa-9"]
    )
    assert rotate.exit_code == 0, rotate.stdout
    cred = store.get_svc_acct(ident.id)
    assert cred.client_secret_plaintext == "new" and cred.key_version == 2

    delete = runner.invoke(
        app, ["team", "ingest-clients", "delete", "globo", "ingest", "sa-9"]
    )
    assert delete.exit_code == 0, delete.stdout
    assert store.list_identities("local", kind="SERVICE_ACCOUNT") == []


@respx.mock
def test_ingest_client_list_no_local_write() -> None:
    _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.upsert_team("local", "globo", "ingest", "t-1")
    _mock_org()
    respx.get(f"{IDP}/v1/teams/t-1/ingest-clients").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "sa-9", "displayName": "edge-01", "status": "ACTIVE", "keyVersion": 1}]})
    )

    result = runner.invoke(
        app, ["team", "ingest-clients", "list", "globo", "ingest"], env={"VL_OUTPUT": "json"}
    )

    assert result.exit_code == 0, result.stdout
    assert "sa-9" in result.stdout
    assert store.list_identities("local", kind="SERVICE_ACCOUNT") == []
