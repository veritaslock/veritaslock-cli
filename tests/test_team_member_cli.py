"""Tests for the `vl team-member` command group."""

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

GLOBO = {"id": "org-1", "name": "globo", "displayName": "Globo", "active": True, "createdAt": "2026-01-01T00:00:00Z"}
FULCRUM = {"id": "org-2", "name": "fulcrum", "displayName": "Fulcrum", "active": True, "createdAt": "2026-01-01T00:00:00Z"}
TEAM = {"id": "t-1", "name": "ingest", "description": "", "orgId": "org-1"}
FULCRUM_TEAM = {"id": "t-2", "name": "ingest", "description": "", "orgId": "org-2"}


def _jwt(orgs: list[dict[str, str]]) -> str:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"orgs": orgs}).encode())
        .decode()
        .rstrip("=")
    )
    return f"h.{payload}.s"


def _caller(orgs: list[dict[str, str]] | None = None) -> store.Identity:
    orgs = orgs or [{"orgId": "org-1", "role": "USER"}]
    store.ensure_local_environment_seeded()
    ident = store.add_identity("local", "USER", "u-root", "root", "root")
    store.set_user_acct(ident.id, "pw")
    store.set_default_identity("local", "root")
    now = datetime.now(timezone.utc)
    store.set_cached_token(ident.id, _jwt(orgs), now, now + timedelta(hours=1))
    return ident


def _cache_globo() -> None:
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True, created_at="2026-01-01T00:00:00Z")


def _mock_team_lookup(org_id: str = "org-1", team: dict | None = None) -> None:
    respx.get(f"{IDP}/v1/teams", params={"orgId": org_id, "name": "ingest"}).mock(
        return_value=httpx.Response(
            200, json={"items": [team or TEAM] if team is not False else []}
        )
    )


def _mock_org_users(org_id: str, users: list[dict]) -> None:
    respx.get(f"{IDP}/v1/users", params={"orgId": org_id}).mock(
        return_value=httpx.Response(200, json={"items": users, "nextCursor": None})
    )


@respx.mock
def test_list_resolves_team_by_name_and_shows_usernames() -> None:
    _caller()
    _cache_globo()
    _mock_team_lookup()
    respx.get(f"{IDP}/v1/teams/t-1/members").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"teamId": "t-1", "userId": "u-9", "role": "TEAM_ADMIN", "addedAt": "x"},
                ]
            },
        )
    )
    _mock_org_users("org-1", [{"id": "u-9", "username": "jdoe"}])

    result = runner.invoke(app, ["team-member", "list", "ingest"], env={"VL_OUTPUT": "json"})

    assert result.exit_code == 0, result.stdout
    rows = json.loads(result.stdout)
    assert rows == [
        {"username": "jdoe", "user_id": "u-9", "role": "TEAM_ADMIN", "added_at": "x"}
    ]


@respx.mock
def test_list_refreshes_local_team_member_cache() -> None:
    _caller()
    _cache_globo()
    store.upsert_team("local", "globo", "ingest", "t-1")
    known = store.add_identity("local", "USER", "u-9", "jdoe", "jdoe")
    _mock_team_lookup()
    respx.get(f"{IDP}/v1/teams/t-1/members").mock(
        return_value=httpx.Response(
            200, json={"items": [{"teamId": "t-1", "userId": "u-9", "role": "TEAM_MEMBER"}]}
        )
    )
    _mock_org_users("org-1", [{"id": "u-9", "username": "jdoe"}])

    result = runner.invoke(app, ["team-member", "list", "ingest"])

    assert result.exit_code == 0, result.stdout
    memberships = store.list_team_memberships(known.id)
    assert [(m.team_name, m.role) for m in memberships] == [("ingest", "TEAM_MEMBER")]


@respx.mock
def test_list_ambiguous_team_name_errors() -> None:
    _caller([{"orgId": "org-1", "role": "USER"}, {"orgId": "org-2", "role": "USER"}])
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True, created_at="2026-01-01T00:00:00Z")
    store.upsert_organization("local", "fulcrum", "org-2", "Fulcrum", active=True, created_at="2026-01-01T00:00:00Z")
    _mock_team_lookup("org-1")
    respx.get(f"{IDP}/v1/teams", params={"orgId": "org-2", "name": "ingest"}).mock(
        return_value=httpx.Response(200, json={"items": [FULCRUM_TEAM]})
    )

    result = runner.invoke(app, ["team-member", "list", "ingest"])

    assert result.exit_code == 1
    assert "More than one team is named 'ingest'" in result.stdout
    assert "--org" in result.stdout


@respx.mock
def test_list_org_option_disambiguates() -> None:
    _caller([{"orgId": "org-1", "role": "USER"}, {"orgId": "org-2", "role": "USER"}])
    respx.get(f"{IDP}/v1/organizations", params={"name": "fulcrum"}).mock(
        return_value=httpx.Response(200, json=FULCRUM)
    )
    respx.get(f"{IDP}/v1/teams", params={"orgId": "org-2", "name": "ingest"}).mock(
        return_value=httpx.Response(200, json={"items": [FULCRUM_TEAM]})
    )
    respx.get(f"{IDP}/v1/teams/t-2/members").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    _mock_org_users("org-2", [])

    result = runner.invoke(
        app, ["team-member", "list", "ingest", "--org", "fulcrum"], env={"VL_OUTPUT": "json"}
    )

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == []


@respx.mock
def test_list_unknown_team_errors() -> None:
    _caller()
    _cache_globo()
    respx.get(f"{IDP}/v1/teams", params={"orgId": "org-1", "name": "nope"}).mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    result = runner.invoke(app, ["team-member", "list", "nope"])

    assert result.exit_code == 1
    assert "No team named 'nope'" in result.stdout


@respx.mock
def test_add_resolves_user_in_org_and_posts() -> None:
    _caller()
    _cache_globo()
    known = store.add_identity("local", "USER", "u-9", "jdoe", "jdoe")
    _mock_team_lookup()
    _mock_org_users("org-1", [{"id": "u-9", "username": "jdoe"}])
    post = respx.post(f"{IDP}/v1/teams/t-1/members").mock(
        return_value=httpx.Response(201, json={"teamId": "t-1", "userId": "u-9", "role": "TEAM_MEMBER"})
    )

    result = runner.invoke(app, ["team-member", "add", "ingest", "jdoe"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(post.calls.last.request.content) == {"userId": "u-9"}
    assert "Added jdoe to globo/ingest as TEAM_MEMBER" in result.stdout
    assert store.list_team_memberships(known.id)[0].role == "TEAM_MEMBER"


@respx.mock
def test_add_role_flag_passed_through() -> None:
    _caller()
    _cache_globo()
    _mock_team_lookup()
    _mock_org_users("org-1", [{"id": "u-9", "username": "jdoe"}])
    post = respx.post(f"{IDP}/v1/teams/t-1/members").mock(
        return_value=httpx.Response(201, json={"teamId": "t-1", "userId": "u-9", "role": "TEAM_ADMIN"})
    )

    result = runner.invoke(
        app, ["team-member", "add", "ingest", "jdoe", "--role", "TEAM_ADMIN"]
    )

    assert result.exit_code == 0, result.stdout
    assert json.loads(post.calls.last.request.content) == {"userId": "u-9", "role": "TEAM_ADMIN"}


@respx.mock
def test_add_user_not_in_teams_org_errors_before_post() -> None:
    _caller()
    _cache_globo()
    _mock_team_lookup()
    _mock_org_users("org-1", [{"id": "u-1", "username": "someone-else"}])
    post = respx.post(f"{IDP}/v1/teams/t-1/members").mock(
        return_value=httpx.Response(201, json={})
    )

    result = runner.invoke(app, ["team-member", "add", "ingest", "jdoe"])

    assert result.exit_code == 1
    assert "No user 'jdoe' in organization 'globo'" in result.stdout
    assert not post.called


@respx.mock
def test_add_platform_admin_searches_every_org() -> None:
    _caller([{"orgId": "org-0", "role": "PLATFORM_ADMIN"}])
    respx.get(f"{IDP}/v1/organizations").mock(
        return_value=httpx.Response(
            200, json={"items": [GLOBO, FULCRUM], "nextCursor": None}
        )
    )
    respx.get(f"{IDP}/v1/teams", params={"orgId": "org-1", "name": "ingest"}).mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    respx.get(f"{IDP}/v1/teams", params={"orgId": "org-2", "name": "ingest"}).mock(
        return_value=httpx.Response(200, json={"items": [FULCRUM_TEAM]})
    )
    _mock_org_users("org-2", [{"id": "u-9", "username": "jdoe"}])
    post = respx.post(f"{IDP}/v1/teams/t-2/members").mock(
        return_value=httpx.Response(201, json={"teamId": "t-2", "userId": "u-9", "role": "TEAM_MEMBER"})
    )

    result = runner.invoke(app, ["team-member", "add", "ingest", "jdoe"])

    assert result.exit_code == 0, result.stdout
    assert post.called
    assert "Added jdoe to fulcrum/ingest" in result.stdout
