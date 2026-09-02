"""Tests for the `vl team` command group."""

from __future__ import annotations

import base64
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


FULCRUM_DTO = {"id": "org-2", "name": "fulcrum", "displayName": "Fulcrum", "active": True}
FULCRUM_TEAM = {**TEAM_DTO, "id": "t-2", "name": "leverage", "orgId": "org-2"}


def _jwt(orgs: list[dict[str, str]]) -> str:
    """A fake but decodable JWT carrying just an ``orgs`` claim."""
    payload = (
        base64.urlsafe_b64encode(json.dumps({"orgs": orgs}).encode())
        .decode()
        .rstrip("=")
    )
    return f"h.{payload}.s"


def _caller_with_orgs(orgs: list[dict[str, str]]) -> store.Identity:
    store.ensure_local_environment_seeded()
    ident = store.add_identity("local", "USER", "u-root", "root", "root")
    store.set_user_acct(ident.id, "pw")
    store.set_default_identity("local", "root")
    now = datetime.now(timezone.utc)
    store.set_cached_token(ident.id, _jwt(orgs), now, now + timedelta(hours=1))
    return ident


@respx.mock
def test_list_org_member_lists_own_orgs_and_caches() -> None:
    _caller_with_orgs([{"orgId": "org-1", "role": "USER"}])
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    route = respx.get(f"{IDP}/v1/teams", params={"orgId": "org-1"}).mock(
        return_value=httpx.Response(200, json={"items": [TEAM_DTO], "nextCursor": None})
    )

    result = runner.invoke(app, ["team", "list"])

    assert result.exit_code == 0, result.stdout
    assert route.called
    assert "ingest" in result.stdout and "globo" in result.stdout
    assert store.list_teams("local", "globo")[0].name == "ingest"


@respx.mock
def test_list_multi_org_member_aggregates_across_its_orgs() -> None:
    _caller_with_orgs(
        [
            {"orgId": "org-1", "role": "USER"},
            {"orgId": "org-2", "role": "KEY_READER"},
        ]
    )
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.upsert_organization("local", "fulcrum", "org-2", "Fulcrum", active=True)
    respx.get(f"{IDP}/v1/teams", params={"orgId": "org-1"}).mock(
        return_value=httpx.Response(200, json={"items": [TEAM_DTO]})
    )
    respx.get(f"{IDP}/v1/teams", params={"orgId": "org-2"}).mock(
        return_value=httpx.Response(200, json={"items": [FULCRUM_TEAM]})
    )

    result = runner.invoke(app, ["team", "list"])

    assert result.exit_code == 0, result.stdout
    assert "ingest" in result.stdout and "leverage" in result.stdout


@respx.mock
def test_list_platform_admin_lists_every_org() -> None:
    _caller_with_orgs([{"orgId": "org-1", "role": "PLATFORM_ADMIN"}])
    respx.get(f"{IDP}/v1/organizations").mock(
        return_value=httpx.Response(
            200, json={"items": [ORG_DTO, FULCRUM_DTO], "nextCursor": None}
        )
    )
    respx.get(f"{IDP}/v1/teams", params={"orgId": "org-1"}).mock(
        return_value=httpx.Response(200, json={"items": [TEAM_DTO]})
    )
    respx.get(f"{IDP}/v1/teams", params={"orgId": "org-2"}).mock(
        return_value=httpx.Response(200, json={"items": [FULCRUM_TEAM]})
    )

    result = runner.invoke(app, ["team", "list"])

    assert result.exit_code == 0, result.stdout
    assert "all organizations" in result.stdout
    assert "ingest" in result.stdout and "leverage" in result.stdout


@respx.mock
def test_list_passes_name_filter_through() -> None:
    _caller_with_orgs([{"orgId": "org-1", "role": "USER"}])
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    route = respx.get(f"{IDP}/v1/teams").mock(
        return_value=httpx.Response(200, json={"items": [TEAM_DTO]})
    )

    result = runner.invoke(app, ["team", "list", "--name", "ingest"])

    assert result.exit_code == 0, result.stdout
    assert route.calls.last.request.url.params["name"] == "ingest"


@respx.mock
def test_list_service_account_caller_sees_nothing() -> None:
    # A token with no `orgs` claim (service account) yields an empty listing.
    store.ensure_local_environment_seeded()
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "sys")
    store.set_default_identity("local", "sys")
    now = datetime.now(timezone.utc)
    store.set_cached_token(ident.id, "h.e30.s", now, now + timedelta(hours=1))

    result = runner.invoke(app, ["team", "list"], env={"VL_OUTPUT": "json"})

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == []


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

