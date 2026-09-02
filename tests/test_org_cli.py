"""Tests for the `vl org` command group (HTTP mocked with respx)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import respx
from typer.testing import CliRunner

from vl.app import app
from vl.lib import store

runner = CliRunner()
IDP = "http://localhost:8080"

ORG_DTO = {
    "id": "org-1",
    "name": "globo",
    "displayName": "Globo Corp",
    "active": True,
    "createdBy": "u-0",
    "owner": "u-0",
}


def _caller() -> store.Identity:
    """A resolved-and-tokened default identity so write commands skip re-auth."""
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


# --------------------------------------------------------------------------- #
# reads (unauthenticated)
# --------------------------------------------------------------------------- #


@respx.mock
def test_show_hits_server_and_caches() -> None:
    _mock_org()
    result = runner.invoke(app, ["org", "show", "globo"], env={"VL_OUTPUT": "json"})
    assert result.exit_code == 0, result.stdout
    assert "org-1" in result.stdout
    assert store.get_organization("local", "globo").display_name == "Globo Corp"


@respx.mock
def test_list_caches_every_row_and_shows_paging_hint() -> None:
    respx.get(f"{IDP}/v1/organizations").mock(
        return_value=httpx.Response(200, json={"items": [ORG_DTO], "nextCursor": "1"})
    )
    result = runner.invoke(app, ["org", "list"])
    assert result.exit_code == 0, result.stdout
    assert "--page 1" in result.stdout
    assert store.list_organizations("local")[0].name == "globo"


# --------------------------------------------------------------------------- #
# writes (authenticated as the resolved identity)
# --------------------------------------------------------------------------- #


@respx.mock
def test_add_creates_and_caches() -> None:
    _caller()
    create = respx.post(f"{IDP}/v1/organizations").mock(
        return_value=httpx.Response(201, json=ORG_DTO)
    )

    result = runner.invoke(app, ["org", "add", "globo", "--display-name", "Globo Corp"])

    assert result.exit_code == 0, result.stdout
    body = create.calls.last.request.content
    assert b'"name":"globo"' in body and b'"displayName":"Globo Corp"' in body
    assert b"initialAdminUserId" not in body
    assert create.calls.last.request.headers["authorization"] == "Bearer caller-jwt"
    assert store.get_organization("local", "globo").server_org_id == "org-1"


@respx.mock
def test_add_with_initial_admin() -> None:
    _caller()
    create = respx.post(f"{IDP}/v1/organizations").mock(
        return_value=httpx.Response(201, json=ORG_DTO)
    )

    result = runner.invoke(
        app,
        ["org", "add", "globo", "--display-name", "Globo", "--initial-admin", "u-42"],
    )

    assert result.exit_code == 0, result.stdout
    assert b'"initialAdminUserId":"u-42"' in create.calls.last.request.content


@respx.mock
def test_add_surfaces_server_conflict_cleanly() -> None:
    _caller()
    respx.post(f"{IDP}/v1/organizations").mock(
        return_value=httpx.Response(
            409, json={"detail": "Organization name already exists.", "errorCode": "CONFLICT"}
        )
    )

    result = runner.invoke(app, ["org", "add", "globo", "--display-name", "Globo"])

    assert result.exit_code == 1
    assert "already exists" in result.stdout
    assert "Traceback" not in result.stdout


def test_add_with_no_identity_errors() -> None:
    store.ensure_local_environment_seeded()
    result = runner.invoke(app, ["org", "add", "globo", "--display-name", "Globo"])
    assert result.exit_code == 1
    assert "No identity selected" in result.stdout


def test_update_requires_a_field() -> None:
    result = runner.invoke(app, ["org", "update", "globo"])
    assert result.exit_code == 1
    assert "nothing to update" in result.stdout


@respx.mock
def test_update_patches_by_name() -> None:
    _caller()
    patch = respx.patch(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json={**ORG_DTO, "active": False})
    )

    result = runner.invoke(
        app, ["org", "update", "globo", "--no-active"], env={"VL_OUTPUT": "json"}
    )

    assert result.exit_code == 0, result.stdout
    assert b'"active":false' in patch.calls.last.request.content


@respx.mock
def test_members_add_resolves_org_id_then_posts() -> None:
    _caller()
    _mock_org()
    add = respx.post(f"{IDP}/v1/organizations/org-1/members").mock(
        return_value=httpx.Response(204)
    )

    result = runner.invoke(
        app, ["org", "members", "add", "globo", "--user-id", "u-9", "--role", "ORG_ADMIN"]
    )

    assert result.exit_code == 0, result.stdout
    body = add.calls.last.request.content
    assert b'"userId":"u-9"' in body and b'"role":"ORG_ADMIN"' in body


@respx.mock
def test_members_add_retrofit_upserts_local_membership() -> None:
    _caller()
    ident = store.add_identity("local", "USER", "u-9", "jdoe", "jdoe")
    _mock_org()
    respx.post(f"{IDP}/v1/organizations/org-1/members").mock(
        return_value=httpx.Response(204)
    )

    result = runner.invoke(
        app, ["org", "members", "add", "globo", "--user-id", "u-9", "--role", "ORG_ADMIN"]
    )

    assert result.exit_code == 0, result.stdout
    assert [(m.org_name, m.role) for m in store.list_org_memberships(ident.id)] == [
        ("globo", "ORG_ADMIN")
    ]


@respx.mock
def test_members_add_accepts_key_reader_role() -> None:
    _caller()
    _mock_org()
    add = respx.post(f"{IDP}/v1/organizations/org-1/members").mock(
        return_value=httpx.Response(204)
    )

    result = runner.invoke(
        app, ["org", "members", "add", "globo", "--user-id", "u-9", "--role", "KEY_READER"]
    )

    assert result.exit_code == 0, result.stdout
    assert b'"role":"KEY_READER"' in add.calls.last.request.content


@respx.mock
def test_members_set_role_patches_and_syncs_local() -> None:
    _caller()
    ident = store.add_identity("local", "USER", "u-9", "jdoe", "jdoe")
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.upsert_org_membership(ident.id, "local", "globo", "USER")
    _mock_org()
    patch = respx.patch(f"{IDP}/v1/organizations/org-1/members/u-9").mock(
        return_value=httpx.Response(200, json={"orgId": "org-1", "userId": "u-9", "role": "ORG_ADMIN"})
    )

    result = runner.invoke(
        app,
        ["org", "members", "set-role", "globo", "u-9", "--role", "ORG_ADMIN"],
    )

    assert result.exit_code == 0, result.stdout
    assert b'"role":"ORG_ADMIN"' in patch.calls.last.request.content
    assert store.list_org_memberships(ident.id)[0].role == "ORG_ADMIN"


@respx.mock
def test_members_set_role_surfaces_last_admin_409() -> None:
    _caller()
    _mock_org()
    respx.patch(f"{IDP}/v1/organizations/org-1/members/u-9").mock(
        return_value=httpx.Response(
            409, json={"detail": "Would leave the organization with no admin.", "errorCode": "CONFLICT"}
        )
    )

    result = runner.invoke(
        app, ["org", "members", "set-role", "globo", "u-9", "--role", "USER"]
    )

    assert result.exit_code == 1
    assert "no admin" in result.stdout


@respx.mock
def test_members_add_no_local_identity_is_not_an_error() -> None:
    _caller()
    _mock_org()
    respx.post(f"{IDP}/v1/organizations/org-1/members").mock(
        return_value=httpx.Response(204)
    )

    result = runner.invoke(
        app,
        ["org", "members", "add", "globo", "--user-id", "u-unknown", "--role", "USER"],
    )

    assert result.exit_code == 0, result.stdout


@respx.mock
def test_members_remove_surfaces_last_membership_conflict() -> None:
    _caller()
    _mock_org()
    respx.delete(f"{IDP}/v1/organizations/org-1/members/u-9").mock(
        return_value=httpx.Response(
            409,
            json={"detail": "Cannot remove the user's only organization membership.", "errorCode": "CONFLICT"},
        )
    )

    result = runner.invoke(
        app, ["org", "members", "remove", "globo", "--user-id", "u-9"]
    )

    assert result.exit_code == 1
    assert "only organization membership" in result.stdout


@respx.mock
def test_members_list_renders_rows() -> None:
    _caller()
    _mock_org()
    respx.get(f"{IDP}/v1/organizations/org-1/members").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"orgId": "org-1", "userId": "u-9", "role": "USER", "addedAt": "2026-08-30T00:00:00Z"}]},
        )
    )

    result = runner.invoke(
        app, ["org", "members", "list", "globo"], env={"VL_OUTPUT": "json"}
    )

    assert result.exit_code == 0, result.stdout
    assert "u-9" in result.stdout
