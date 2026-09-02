"""Tests for the `vl usr-acct` command group."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import bcrypt
import httpx
import pytest
import respx
from typer.testing import CliRunner

from vl.app import app
from vl.lib import store

from _helpers import fake_jwt

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
        app, ["usr-acct", "add", "jdoe", "--email", "jdoe@x.com", "--org", "globo"]
    )

    assert result.exit_code == 0, result.stdout
    body = json.loads(create.calls.last.request.content)
    assert body["username"] == "jdoe"
    assert body["email"] == "jdoe@x.com"
    assert "displayName" not in body  # no --first / --last
    assert "phoneNumber" not in body  # omitted when --phone not given
    # --role omitted -> defaults to USER
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
    assert "globo:USER" in result.stdout  # org shown in the summary
    assert "Password (generated, shown once)" in result.stdout


@respx.mock
def test_add_sends_name_email_and_phone() -> None:
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
            "usr-acct", "add", "jdoe", "--org", "globo", "--role", "ORG_ADMIN",
            "--first", "John", "--last", "Doe",
            "--email", "j@real.com", "--phone", "+15555550123",
        ],
        env={"VL_OUTPUT": "json"},
    )

    assert result.exit_code == 0, result.stdout
    body = json.loads(create.calls.last.request.content)
    assert body["email"] == "j@real.com"
    assert body["phoneNumber"] == "+15555550123"
    assert body["displayName"] == "John Doe"  # joined from --first / --last
    summary = json.loads(result.stdout.split("Password")[0])
    assert summary["phone"] == "+15555550123"
    assert summary["org"] == "globo:ORG_ADMIN"


@respx.mock
def test_add_uses_supplied_password_and_stays_quiet() -> None:
    _caller()
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    create = respx.post(f"{IDP}/v1/users").mock(
        return_value=httpx.Response(
            201, json={"id": "u-new", "username": "jdoe", "email": "j@x", "status": "ACTIVE"}
        )
    )

    result = runner.invoke(
        app,
        ["usr-acct", "add", "jdoe", "--email", "j@x.com", "--org", "globo",
         "--password", "correct horse battery"],
    )

    assert result.exit_code == 0, result.stdout
    body = json.loads(create.calls.last.request.content)
    assert bcrypt.checkpw(b"correct horse battery", body["passwordHash"].encode())
    cred = store.get_user_acct(store.get_identity("local", "jdoe").id)
    assert cred is not None and cred.password_plaintext == "correct horse battery"
    assert "generated" not in result.stdout  # nothing printed — the user chose it


def test_add_requires_email() -> None:
    _caller()
    result = runner.invoke(app, ["usr-acct", "add", "jdoe", "--org", "globo"])
    # a missing required option -> `add` help, exit 0, no server call attempted
    assert result.exit_code == 0
    assert "--email" in result.output and "Error" not in result.output


@respx.mock
def test_add_only_first_name_still_sets_display_name() -> None:
    _caller()
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    create = respx.post(f"{IDP}/v1/users").mock(
        return_value=httpx.Response(
            201, json={"id": "u-new", "username": "cher", "email": "c@x", "status": "ACTIVE"}
        )
    )

    result = runner.invoke(
        app, ["usr-acct", "add", "cher", "--email", "c@x.com", "--org", "globo", "--first", "Cher"]
    )

    assert result.exit_code == 0, result.stdout
    assert json.loads(create.calls.last.request.content)["displayName"] == "Cher"


@respx.mock
def test_add_without_name_renders_blank_display_name_not_none() -> None:
    _caller()
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    # server echoes displayName back as an explicit null
    respx.post(f"{IDP}/v1/users").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "u-new", "username": "jdoe", "email": "j@x",
                "displayName": None, "status": "ACTIVE",
            },
        )
    )

    result = runner.invoke(
        app, ["usr-acct", "add", "jdoe", "--email", "j@x.com", "--org", "globo"]
    )

    assert result.exit_code == 0, result.stdout
    assert "None" not in result.stdout


@respx.mock
def test_add_rejects_service_account_caller() -> None:
    _caller("svc", kind="SERVICE_ACCOUNT")

    result = runner.invoke(
        app, ["usr-acct", "add", "jdoe", "--email", "j@x.com", "--org", "globo"]
    )
    assert result.exit_code == 1
    assert "only a USER identity" in result.stdout


def test_add_with_no_identity_errors() -> None:
    store.ensure_local_environment_seeded()  # no identities at all
    result = runner.invoke(
        app, ["usr-acct", "add", "jdoe", "--email", "j@x.com", "--org", "globo"]
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
        app, ["usr-acct", "add", "jdoe", "--email", "j@x.com", "--org", "globo", "--role", "ORG_ADMIN"]
    )
    assert result.exit_code == 1
    assert "Not authorized to create users" in result.stdout


def test_show_local_by_default_masks_secret() -> None:
    _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    target = store.add_identity("local", "USER", "u-5", "jdoe", "jdoe")
    store.set_user_acct(target.id, "local-pw")
    store.upsert_org_membership(target.id, "local", "globo", "USER")

    # No respx mock — the local view must not hit the server.
    masked = runner.invoke(app, ["usr-acct", "show", "jdoe"], env={"VL_OUTPUT": "json"})
    assert masked.exit_code == 0, masked.stdout
    assert "local-pw" not in masked.stdout and "globo:USER" in masked.stdout

    revealed = runner.invoke(
        app, ["usr-acct", "show", "jdoe", "--reveal-secret"], env={"VL_OUTPUT": "json"}
    )
    assert "local-pw" in revealed.stdout


@respx.mock
def test_show_all_merges_server_and_local() -> None:
    _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    target = store.add_identity("local", "USER", "u-5", "jdoe", "jdoe")
    store.set_user_acct(target.id, "local-pw")
    store.upsert_org_membership(target.id, "local", "globo", "USER")
    respx.get(f"{IDP}/v1/users/u-5").mock(
        return_value=httpx.Response(
            200,
            json={"id": "u-5", "username": "jdoe", "email": "j@example.com", "displayName": "J Doe", "status": "SUSPENDED", "mfaEnabled": False},
        )
    )

    result = runner.invoke(
        app, ["usr-acct", "show", "jdoe", "--all"], env={"VL_OUTPUT": "json"}
    )
    assert result.exit_code == 0, result.stdout
    assert "SUSPENDED" in result.stdout  # server field
    assert "globo:USER" in result.stdout  # local field


@pytest.mark.parametrize("noun", ["usr-acct", "user", "user-acct"])
def test_command_noun_synonyms(noun: str) -> None:
    _caller()
    ident = store.add_identity("local", "USER", "u-5", "jdoe", "jdoe")
    store.set_user_acct(ident.id, "pw")

    result = runner.invoke(app, [noun, "list"], env={"VL_OUTPUT": "json"})

    assert result.exit_code == 0, result.stdout
    assert "jdoe" in result.stdout
    assert "deprecat" not in result.stdout.lower() and "synonym" not in result.stdout.lower()


def test_synonyms_hidden_from_top_level_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert "usr-acct" in result.stdout
    assert "user-acct" not in result.stdout
    # "user" only ever appears inside other words (e.g. "user accounts"), never as
    # a standalone command entry.
    assert not any(
        line.strip().startswith("user ") or line.strip().startswith("user-acct")
        for line in result.stdout.splitlines()
    )


@pytest.mark.parametrize("argv", [["usr-acct", "nope"], ["user", "nope"], ["nope"]])
def test_unknown_subcommand_prints_help_like_dash_dash_help(argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == 0  # behaves exactly like --help
    assert "Usage:" in result.output and "Commands" in result.output
    assert "Error" not in result.output and "No such command" not in result.output
    if argv[:1] != ["nope"]:
        assert "Manage VeritasLock user accounts." in result.output  # group's own help


def test_leaf_command_missing_arg_prints_command_help() -> None:
    result = runner.invoke(app, ["usr-acct", "login"])  # `login` needs <username>
    assert result.exit_code == 0
    assert "Authenticate as yourself" in result.output  # the command's own help
    assert "username" in result.output
    assert "Error" not in result.output and "Missing argument" not in result.output


def test_list_local_by_default() -> None:
    _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    ident = store.add_identity("local", "USER", "u-5", "jdoe", "jdoe")
    store.set_user_acct(ident.id, "pw")

    # No respx mock — a server call would fail. Local listing must not make one.
    result = runner.invoke(app, ["usr-acct", "list"], env={"VL_OUTPUT": "json"})

    assert result.exit_code == 0, result.stdout
    assert "jdoe" in result.stdout and "root *" in result.stdout  # default marked


def test_list_local_filtered_by_org() -> None:
    _caller()
    for name in ("globo", "acme"):
        store.upsert_organization("local", name, f"o-{name}", name, active=True)
    a = store.add_identity("local", "USER", "u-a", "aa", "aa")
    store.set_user_acct(a.id, "pw")
    store.upsert_org_membership(a.id, "local", "globo", "USER")
    b = store.add_identity("local", "USER", "u-b", "bb", "bb")
    store.set_user_acct(b.id, "pw")
    store.upsert_org_membership(b.id, "local", "acme", "USER")

    result = runner.invoke(
        app, ["usr-acct", "list", "--org", "globo"], env={"VL_OUTPUT": "json"}
    )

    assert result.exit_code == 0, result.stdout
    assert "aa" in result.stdout
    assert "bb" not in result.stdout
    assert "root" not in result.stdout  # no membership in globo


@respx.mock
def test_list_remote_resolves_org_to_orgid_filter() -> None:
    _caller()
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    route = respx.get(f"{IDP}/v1/users").mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    result = runner.invoke(
        app, ["usr-acct", "list", "--remote", "--org", "globo"],
        env={"VL_OUTPUT": "json"},
    )

    assert result.exit_code == 0, result.stdout
    assert route.calls.last.request.url.params["orgId"] == "org-1"


@respx.mock
def test_list_remote_passes_filters_and_marks_local() -> None:
    _caller()
    store.add_identity("local", "USER", "u-1", "a", "alpha")  # cached
    route = respx.get(f"{IDP}/v1/users").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"id": "u-1", "username": "a", "email": "a@x", "status": "ACTIVE"},
                    {"id": "u-2", "username": "b", "email": "b@x", "status": "ACTIVE"},
                ],
                "nextCursor": "1",
            },
        )
    )

    result = runner.invoke(
        app, ["usr-acct", "list", "--remote", "--status", "ACTIVE"],
        env={"VL_OUTPUT": "json"},
    )

    assert result.exit_code == 0, result.stdout
    assert route.calls.last.request.url.params["status"] == "ACTIVE"
    # JSON mode: the pagination hint is suppressed so stdout stays parseable.
    rows = json.loads(result.stdout)
    assert {r["server_id"]: r["cached"] for r in rows} == {"u-1": "yes", "u-2": "no"}
    assert "more results" not in result.stdout


@respx.mock
def test_list_remote_pagination_hint_shown_in_table_mode() -> None:
    _caller()
    respx.get(f"{IDP}/v1/users").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"id": "u-1", "username": "a", "email": "a@x", "status": "ACTIVE"},
                ],
                "nextCursor": "1",
            },
        )
    )

    result = runner.invoke(app, ["usr-acct", "list", "--remote"])

    assert result.exit_code == 0, result.stdout
    assert "--page 1" in result.stdout


@respx.mock
def test_list_remote_orgs_column_from_local_cache() -> None:
    _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.upsert_organization("local", "acme", "org-2", "Acme", active=True)
    cached = store.add_identity("local", "USER", "u-1", "a", "alpha")
    store.upsert_org_membership(cached.id, "local", "globo", "ORG_ADMIN")
    store.upsert_org_membership(cached.id, "local", "acme", "USER")
    respx.get(f"{IDP}/v1/users").mock(
        return_value=httpx.Response(200, json={"items": [
            {"id": "u-1", "username": "a", "email": "a@x", "status": "ACTIVE"},
            {"id": "u-2", "username": "b", "email": "b@x", "status": "ACTIVE"},
        ]})
    )

    result = runner.invoke(
        app, ["usr-acct", "list", "--remote"], env={"VL_OUTPUT": "json"}
    )

    assert result.exit_code == 0, result.stdout
    rows = json.loads(result.stdout)
    assert {r["server_id"]: r["orgs"] for r in rows} == {
        "u-1": "acme:USER, globo:ORG_ADMIN",  # every cached membership, comma-separated
        "u-2": "-",  # not cached — server list has no org data
    }


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
        ["usr-acct", "update", "jdoe", "--display-name", "New Name", "--password", "new-pw"],
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

    result = runner.invoke(app, ["usr-acct", "update", "jdoe", "--password", "new-pw"])

    assert result.exit_code == 0, result.stdout
    assert store.get_user_acct(target.id).password_plaintext is None


@respx.mock
def test_update_sends_phone() -> None:
    _caller()
    store.add_identity("local", "USER", "u-5", "jdoe", "jdoe")
    patch = respx.patch(f"{IDP}/v1/users/u-5").mock(
        return_value=httpx.Response(200, json={"id": "u-5", "username": "jdoe", "email": "j@x", "phoneNumber": "+15555550199", "displayName": "J", "status": "ACTIVE", "mfaEnabled": False})
    )

    result = runner.invoke(app, ["usr-acct", "update", "jdoe", "--phone", "+15555550199"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(patch.calls.last.request.content) == {"phoneNumber": "+15555550199"}


def test_update_nothing_supplied_errors() -> None:
    _caller()
    store.add_identity("local", "USER", "u-5", "jdoe", "jdoe")
    result = runner.invoke(app, ["usr-acct", "update", "jdoe"])
    assert result.exit_code == 1
    assert "nothing to update" in result.stdout


@respx.mock
def test_delete_removes_local_identity() -> None:
    _caller()
    store.add_identity("local", "USER", "u-5", "jdoe", "jdoe")
    route = respx.delete(f"{IDP}/v1/users/u-5").mock(return_value=httpx.Response(204))

    result = runner.invoke(app, ["usr-acct", "delete", "jdoe"])

    assert result.exit_code == 0, result.stdout
    assert route.called
    assert [i.label for i in store.list_identities("local", kind="USER")] == ["root"]


# --------------------------------------------------------------------------- #
# credential management (folded in from `vl identity`)
# --------------------------------------------------------------------------- #


@respx.mock
def test_cache_populates_memberships_from_token(monkeypatch) -> None:
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "pw")
    respx.post(f"{IDP}/auth/user/login").mock(
        return_value=httpx.Response(
            200,
            json={
                "accessToken": fake_jwt("u-7", orgs=[{"orgId": "org-1", "role": "ORG_ADMIN"}]),
                "expiresIn": 900,
            },
        )
    )
    respx.get(f"{IDP}/v1/organizations/org-1").mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )

    result = runner.invoke(
        app, ["usr-acct", "cache", "--username", "admin"]
    )

    assert result.exit_code == 0, result.stdout
    ident = store.get_identity("local", "admin")  # label == username
    assert ident.server_id == "u-7"
    assert store.get_user_acct(ident.id).password_plaintext == "pw"
    m = store.list_org_memberships(ident.id)[0]
    assert (m.org_name, m.role) == ("globo", "ORG_ADMIN")


@respx.mock
def test_cache_bad_credentials_stores_nothing(monkeypatch) -> None:
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "wrong")
    respx.post(f"{IDP}/auth/user/login").mock(
        return_value=httpx.Response(401, json={"detail": "Bad credentials.", "errorCode": "UNAUTHORIZED"})
    )
    result = runner.invoke(app, ["usr-acct", "cache", "--username", "admin"])
    assert result.exit_code == 1
    assert "Bad credentials" in result.stdout
    assert store.list_identities("local") == []


@respx.mock
def test_cache_refuses_duplicate_account(monkeypatch) -> None:
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "pw")
    store.ensure_local_environment_seeded()
    store.add_identity("local", "USER", "u-7", "admin", "admin")
    respx.post(f"{IDP}/auth/user/login").mock(
        return_value=httpx.Response(200, json={"accessToken": fake_jwt("u-7"), "expiresIn": 900})
    )
    result = runner.invoke(app, ["usr-acct", "cache", "--username", "admin"])
    assert result.exit_code == 1
    assert "already cached as 'admin'" in result.stdout
    assert [i.label for i in store.list_identities("local")] == ["admin"]


@respx.mock
def test_login_is_token_only(monkeypatch) -> None:
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "my-pw")
    respx.post(f"{IDP}/auth/user/login").mock(
        return_value=httpx.Response(200, json={"accessToken": fake_jwt("u-9"), "expiresIn": 900})
    )

    result = runner.invoke(app, ["usr-acct", "login", "alice"])

    assert result.exit_code == 0, result.stdout
    ident = store.get_identity_by_principal("local", "alice", "USER")
    assert store.get_user_acct(ident.id).password_plaintext is None
    assert store.get_cached_token(ident.id) is not None


def test_use_and_clear() -> None:
    store.ensure_local_environment_seeded()
    ident = store.add_identity("local", "USER", "u-1", "alice", "alice")
    store.set_user_acct(ident.id, "sekret")

    assert runner.invoke(app, ["usr-acct", "use", "alice"]).exit_code == 0
    assert store.resolve_identity("local", None).label == "alice"

    result = runner.invoke(app, ["usr-acct", "clear", "alice"])
    assert result.exit_code == 0
    assert "server account is untouched" in result.stdout
    assert store.list_identities("local") == []


def test_use_rejects_service_account() -> None:
    store.ensure_local_environment_seeded()
    store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "sys")
    result = runner.invoke(app, ["usr-acct", "use", "sys"])
    assert result.exit_code == 1
    assert "not a user" in result.stdout


@respx.mock
def test_add_defaults_org_to_callers_org() -> None:
    caller = _caller()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    store.upsert_org_membership(caller.id, "local", "globo", "ORG_ADMIN")
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    create = respx.post(f"{IDP}/v1/users").mock(
        return_value=httpx.Response(201, json={"id": "u-new", "username": "jdoe", "email": "j@x", "displayName": "J D", "status": "ACTIVE"})
    )

    result = runner.invoke(app, ["usr-acct", "add", "jdoe", "--email", "j@x.com"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(create.calls.last.request.content)["organizations"][0]["orgName"] == "globo"


def test_add_errors_when_org_ambiguous() -> None:
    caller = _caller()
    for n in ("globo", "acme"):
        store.upsert_organization("local", n, f"o-{n}", n, active=True)
        store.upsert_org_membership(caller.id, "local", n, "USER")

    result = runner.invoke(app, ["usr-acct", "add", "jdoe", "--email", "j@x.com"])
    assert result.exit_code == 1
    assert "no --org given" in result.stdout
