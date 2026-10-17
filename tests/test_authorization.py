"""Tests for the operator role checks in vl.commands._shared (vl-authorization-spec.md)."""

from __future__ import annotations

from typing import Any

import pytest
import respx

from _helpers import add_service_account, add_user
from vl.commands._shared import CliError, is_org_admin, is_platform_admin, require_operator
from vl.lib import store

GLOBO = "org-1"
ACME = "org-2"


def _claims(*entries: tuple[str, str]) -> dict[str, Any]:
    return {"typ": "user", "orgs": [{"orgId": o, "role": r} for o, r in entries]}


def _env() -> store.Environment:
    store.ensure_local_environment_seeded()
    for name, org_id in (("globo", GLOBO), ("acme", ACME)):
        store.upsert_organization("local", name, org_id, name.title(), True, "2026-01-01T00:00:00Z")
    return store.get_environment(None)


# --------------------------------------------------------------------------- #
# is_platform_admin / is_org_admin
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("claims", "platform", "globo_admin"),
    [
        (_claims((GLOBO, "ORG_ADMIN")), False, True),
        (_claims((GLOBO, "USER")), False, False),
        (_claims((GLOBO, "KEY_READER")), False, False),
        (_claims((GLOBO, "PLATFORM_ADMIN")), True, True),
        (_claims((ACME, "ORG_ADMIN")), False, False),                 # non-matching orgId
        (_claims(("veritaslock", "PLATFORM_ADMIN")), True, True),     # unrelated orgId
        (_claims((ACME, "USER"), (GLOBO, "ORG_ADMIN")), False, True),  # several entries
        (_claims((GLOBO, "USER"), (ACME, "ORG_ADMIN")), False, False),
        (_claims((GLOBO, "USER"), (ACME, "PLATFORM_ADMIN")), True, True),
        (_claims(), False, False),                                    # no entries
        ({"typ": "user"}, False, False),                              # no orgs claim
        ({"orgs": "not-a-list"}, False, False),
        ({"orgs": ["junk", {"role": "PLATFORM_ADMIN"}]}, True, True),  # no orgId still counts
        ({"teams": [{"teamId": "t-1", "role": "TEAM_ADMIN"}], "orgs": []}, False, False),
    ],
)
def test_role_predicates(claims: dict[str, Any], platform: bool, globo_admin: bool) -> None:
    assert is_platform_admin(claims) is platform
    assert is_org_admin(claims, GLOBO) is globo_admin


# --------------------------------------------------------------------------- #
# require_operator: accepting
# --------------------------------------------------------------------------- #


def test_platform_admin_passes_network_and_every_org() -> None:
    env = _env()
    ident = add_user(orgs=[{"orgId": "veritaslock", "role": "PLATFORM_ADMIN"}])
    assert require_operator(env, None).id == ident.id
    assert require_operator(env, None, "globo").id == ident.id
    assert require_operator(env, None, "acme").id == ident.id


def test_org_admin_passes_for_its_own_org_only() -> None:
    env = _env()
    add_user(orgs=[{"orgId": GLOBO, "role": "ORG_ADMIN"}])
    assert require_operator(env, None, "globo").label == "ops"
    with pytest.raises(CliError, match="'ops' is not an admin of 'acme'"):
        require_operator(env, None, "acme")


def test_as_label_picks_the_identity() -> None:
    env = _env()
    add_user("ops", orgs=[{"orgId": GLOBO, "role": "USER"}])
    add_user("boss", orgs=[{"orgId": GLOBO, "role": "ORG_ADMIN"}], default=False)
    assert require_operator(env, "boss", "globo").label == "boss"


# --------------------------------------------------------------------------- #
# require_operator: refusing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", ["USER", "KEY_READER"])
def test_non_admin_roles_are_refused_for_an_org(role: str) -> None:
    env = _env()
    add_user(orgs=[{"orgId": GLOBO, "role": role}])
    with pytest.raises(CliError) as exc:
        require_operator(env, None, "globo")
    assert str(exc.value) == (
        "'ops' is not an admin of 'globo': this needs ORG_ADMIN for the org, or PLATFORM_ADMIN."
    )


@pytest.mark.parametrize("role", ["USER", "ORG_ADMIN", "KEY_READER"])
def test_network_commands_need_platform_admin(role: str) -> None:
    env = _env()
    add_user(orgs=[{"orgId": GLOBO, "role": role}])
    with pytest.raises(CliError) as exc:
        require_operator(env, None)
    assert str(exc.value) == "'ops' needs PLATFORM_ADMIN for network commands."


@pytest.mark.parametrize("role", ["NODE", "SYSTEM"])
@pytest.mark.parametrize("org", [None, "globo"])
def test_service_accounts_are_refused_without_minting_a_token(role: str, org: str | None) -> None:
    env = _env()
    add_service_account("globo-1", "globo", role)
    store.clear_cached_token(store.resolve_identity("local", "globo-1").id)
    with respx.mock:  # no routes: any token request would fail the test
        with pytest.raises(CliError, match="service accounts can't run this command"):
            require_operator(env, "globo-1", org)


def test_a_user_identity_holding_a_service_account_token_is_refused() -> None:
    env = _env()
    add_user(orgs=[{"orgId": GLOBO, "role": "PLATFORM_ADMIN"}], typ="service-account")
    with pytest.raises(CliError, match="service accounts can't run this command"):
        require_operator(env, None, "globo")


def test_a_token_without_typ_is_refused_even_with_platform_admin() -> None:
    env = _env()
    add_user(orgs=[{"orgId": GLOBO, "role": "PLATFORM_ADMIN"}], typ=None)
    with pytest.raises(CliError, match="is not a user token"):
        require_operator(env, None)


def test_an_undecodable_token_is_refused() -> None:
    env = _env()
    add_user(token="not-a-jwt")
    with pytest.raises(CliError, match="The token for 'ops' could not be read"):
        require_operator(env, None, "globo")


def test_no_identity_is_refused_with_the_current_command_names() -> None:
    env = _env()
    with pytest.raises(store.NoResolvedIdentityError) as exc:
        require_operator(env, None, "globo")
    assert "vl usr-acct use" in str(exc.value) and "vl svc-acct use" in str(exc.value)
    assert "vl identity use" not in str(exc.value)


def test_an_org_not_cached_fails_before_any_role_check() -> None:
    env = _env()
    add_user(orgs=[])  # would also fail the role check
    with pytest.raises(store.OrganizationNotFoundError, match="vl org show zeta"):
        require_operator(env, None, "zeta")


def test_a_refusal_does_no_work() -> None:
    env = _env()
    ident = add_user(orgs=[{"orgId": GLOBO, "role": "USER"}])
    token_before = store.get_cached_token(ident.id)
    with respx.mock:  # no routes: any HTTP call would fail the test
        with pytest.raises(CliError):
            require_operator(env, None, "globo")
    assert store.get_cached_token(ident.id) == token_before
