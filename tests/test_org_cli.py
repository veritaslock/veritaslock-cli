"""Tests for the `vl org` command group (HTTP mocked with respx)."""

from __future__ import annotations

import httpx
import respx
from typer.testing import CliRunner

from vl.app import app
from vl.lib import store

runner = CliRunner()
IDP = "http://localhost:8080"  # the auto-seeded `local` environment's idp_base_url

ORG_DTO = {
    "id": "org-1",
    "name": "globo",
    "displayName": "Globo Corp",
    "active": True,
    "createdBy": "u-0",
    "owner": "u-0",
}
LOGIN_OK = httpx.Response(200, json={"accessToken": "jwt-abc"})


@respx.mock
def test_show_hits_server_and_caches() -> None:
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )

    result = runner.invoke(app, ["org", "show", "globo"], env={"VL_OUTPUT": "json"})

    assert result.exit_code == 0, result.stdout
    assert "org-1" in result.stdout
    assert store.get_organization("local", "globo").display_name == "Globo Corp"


@respx.mock
def test_list_caches_every_row_and_shows_paging_hint() -> None:
    respx.get(f"{IDP}/v1/organizations").mock(
        return_value=httpx.Response(
            200, json={"items": [ORG_DTO], "nextCursor": "1"}
        )
    )

    result = runner.invoke(app, ["org", "list"])

    assert result.exit_code == 0, result.stdout
    assert "--page 1" in result.stdout
    assert store.list_organizations("local")[0].name == "globo"


@respx.mock
def test_add_logs_in_then_creates_and_caches() -> None:
    login = respx.post(f"{IDP}/auth/user/login").mock(return_value=LOGIN_OK)
    create = respx.post(f"{IDP}/v1/organizations").mock(
        return_value=httpx.Response(201, json=ORG_DTO)
    )

    result = runner.invoke(
        app,
        [
            "org", "add", "globo",
            "--display-name", "Globo Corp",
            "--auth-user", "alice",
            "--auth-password", "pw",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert login.called and create.called
    body = create.calls.last.request.content
    assert b'"name":"globo"' in body and b'"displayName":"Globo Corp"' in body
    assert b"initialAdminUserId" not in body  # omitted unless --initial-admin given
    assert create.calls.last.request.headers["authorization"] == "Bearer jwt-abc"
    assert store.get_organization("local", "globo").server_org_id == "org-1"


@respx.mock
def test_add_with_initial_admin() -> None:
    respx.post(f"{IDP}/auth/user/login").mock(return_value=LOGIN_OK)
    create = respx.post(f"{IDP}/v1/organizations").mock(
        return_value=httpx.Response(201, json=ORG_DTO)
    )

    result = runner.invoke(
        app,
        [
            "org", "add", "globo", "--display-name", "Globo",
            "--initial-admin", "u-42",
            "--auth-user", "alice", "--auth-password", "pw",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert b'"initialAdminUserId":"u-42"' in create.calls.last.request.content


@respx.mock
def test_add_surfaces_server_conflict_cleanly() -> None:
    respx.post(f"{IDP}/auth/user/login").mock(return_value=LOGIN_OK)
    respx.post(f"{IDP}/v1/organizations").mock(
        return_value=httpx.Response(
            409, json={"detail": "Organization name already exists.", "errorCode": "CONFLICT"}
        )
    )

    result = runner.invoke(
        app,
        [
            "org", "add", "globo",
            "--display-name", "Globo",
            "--auth-user", "alice", "--auth-password", "pw",
        ],
    )

    assert result.exit_code == 1
    assert "already exists" in result.stdout
    assert "Traceback" not in result.stdout


@respx.mock
def test_update_requires_a_field() -> None:
    result = runner.invoke(
        app,
        ["org", "update", "globo", "--auth-user", "alice", "--auth-password", "pw"],
    )
    assert result.exit_code == 1
    assert "nothing to update" in result.stdout


@respx.mock
def test_update_patches_by_name() -> None:
    respx.post(f"{IDP}/auth/user/login").mock(return_value=LOGIN_OK)
    patch = respx.patch(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json={**ORG_DTO, "active": False})
    )

    result = runner.invoke(
        app,
        [
            "org", "update", "globo", "--no-active",
            "--auth-user", "alice", "--auth-password", "pw",
        ],
        env={"VL_OUTPUT": "json"},
    )

    assert result.exit_code == 0, result.stdout
    assert patch.called
    assert b'"active":false' in patch.calls.last.request.content


@respx.mock
def test_members_add_resolves_org_id_then_posts() -> None:
    respx.post(f"{IDP}/auth/user/login").mock(return_value=LOGIN_OK)
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    add = respx.post(f"{IDP}/v1/organizations/org-1/members").mock(
        return_value=httpx.Response(204)
    )

    result = runner.invoke(
        app,
        [
            "org", "members", "add", "globo",
            "--user-id", "u-9", "--role", "ORG_ADMIN",
            "--auth-user", "alice", "--auth-password", "pw",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert add.called
    body = add.calls.last.request.content
    assert b'"userId":"u-9"' in body and b'"role":"ORG_ADMIN"' in body


@respx.mock
def test_members_add_retrofit_upserts_local_membership() -> None:
    # A local identity for the user being granted membership -> §9.6 retrofit fires.
    ident = store.add_identity("local", "USER", "u-9", "jdoe", "jdoe")
    respx.post(f"{IDP}/auth/user/login").mock(return_value=LOGIN_OK)
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    respx.post(f"{IDP}/v1/organizations/org-1/members").mock(
        return_value=httpx.Response(204)
    )

    result = runner.invoke(
        app,
        [
            "org", "members", "add", "globo",
            "--user-id", "u-9", "--role", "ORG_ADMIN",
            "--auth-user", "alice", "--auth-password", "pw",
        ],
    )

    assert result.exit_code == 0, result.stdout
    memberships = store.list_org_memberships(ident.id)
    assert [(m.org_name, m.role) for m in memberships] == [("globo", "ORG_ADMIN")]


@respx.mock
def test_members_add_accepts_key_reader_role() -> None:
    respx.post(f"{IDP}/auth/user/login").mock(return_value=LOGIN_OK)
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    add = respx.post(f"{IDP}/v1/organizations/org-1/members").mock(
        return_value=httpx.Response(204)
    )

    result = runner.invoke(
        app,
        [
            "org", "members", "add", "globo",
            "--user-id", "u-9", "--role", "KEY_READER",
            "--auth-user", "alice", "--auth-password", "pw",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert b'"role":"KEY_READER"' in add.calls.last.request.content


@respx.mock
def test_members_set_role_patches_and_syncs_local() -> None:
    ident = store.add_identity("local", "USER", "u-9", "jdoe", "jdoe")
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.upsert_org_membership(ident.id, "local", "globo", "USER")
    respx.post(f"{IDP}/auth/user/login").mock(return_value=LOGIN_OK)
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    patch = respx.patch(f"{IDP}/v1/organizations/org-1/members/u-9").mock(
        return_value=httpx.Response(200, json={"orgId": "org-1", "userId": "u-9", "role": "ORG_ADMIN"})
    )

    result = runner.invoke(
        app,
        [
            "org", "members", "set-role", "globo", "u-9", "--role", "ORG_ADMIN",
            "--auth-user", "alice", "--auth-password", "pw",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert b'"role":"ORG_ADMIN"' in patch.calls.last.request.content
    assert store.list_org_memberships(ident.id)[0].role == "ORG_ADMIN"


@respx.mock
def test_members_set_role_surfaces_last_admin_409() -> None:
    respx.post(f"{IDP}/auth/user/login").mock(return_value=LOGIN_OK)
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    respx.patch(f"{IDP}/v1/organizations/org-1/members/u-9").mock(
        return_value=httpx.Response(
            409, json={"detail": "Would leave the organization with no admin.", "errorCode": "CONFLICT"}
        )
    )

    result = runner.invoke(
        app,
        [
            "org", "members", "set-role", "globo", "u-9", "--role", "USER",
            "--auth-user", "alice", "--auth-password", "pw",
        ],
    )

    assert result.exit_code == 1
    assert "no admin" in result.stdout


@respx.mock
def test_members_add_no_local_identity_is_not_an_error() -> None:
    respx.post(f"{IDP}/auth/user/login").mock(return_value=LOGIN_OK)
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    respx.post(f"{IDP}/v1/organizations/org-1/members").mock(
        return_value=httpx.Response(204)
    )

    result = runner.invoke(
        app,
        [
            "org", "members", "add", "globo",
            "--user-id", "u-unknown", "--role", "USER",
            "--auth-user", "alice", "--auth-password", "pw",
        ],
    )

    assert result.exit_code == 0, result.stdout


@respx.mock
def test_members_remove_surfaces_last_membership_conflict() -> None:
    respx.post(f"{IDP}/auth/user/login").mock(return_value=LOGIN_OK)
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    respx.delete(f"{IDP}/v1/organizations/org-1/members/u-9").mock(
        return_value=httpx.Response(
            409,
            json={"detail": "Cannot remove the user's only organization membership.", "errorCode": "CONFLICT"},
        )
    )

    result = runner.invoke(
        app,
        [
            "org", "members", "remove", "globo",
            "--user-id", "u-9",
            "--auth-user", "alice", "--auth-password", "pw",
        ],
    )

    assert result.exit_code == 1
    assert "only organization membership" in result.stdout


@respx.mock
def test_members_list_renders_rows() -> None:
    respx.post(f"{IDP}/auth/user/login").mock(return_value=LOGIN_OK)
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    respx.get(f"{IDP}/v1/organizations/org-1/members").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"orgId": "org-1", "userId": "u-9", "role": "USER", "addedAt": "2026-08-30T00:00:00Z"}]},
        )
    )

    result = runner.invoke(
        app,
        ["org", "members", "list", "globo", "--auth-user", "alice", "--auth-password", "pw"],
        env={"VL_OUTPUT": "json"},
    )

    assert result.exit_code == 0, result.stdout
    assert "u-9" in result.stdout


@respx.mock
def test_password_is_prompted_when_omitted() -> None:
    login = respx.post(f"{IDP}/auth/user/login").mock(return_value=LOGIN_OK)
    respx.post(f"{IDP}/v1/organizations").mock(
        return_value=httpx.Response(201, json=ORG_DTO)
    )

    result = runner.invoke(
        app,
        ["org", "add", "globo", "--display-name", "Globo", "--auth-user", "alice"],
        input="secret-pw\n",
    )

    assert result.exit_code == 0, result.stdout
    assert b'"password":"secret-pw"' in login.calls.last.request.content
