"""Tests for the `vl org` command group (HTTP mocked with respx)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import respx
from typer.testing import CliRunner

from _helpers import fake_jwt
from vl.app import app
from vl.lib import store

runner = CliRunner()
IDP = "http://localhost:8080"
# Decodable, so commands that check the caller's role can read it.
CALLER_JWT = fake_jwt("u-root", typ="user", orgs=[{"orgId": "org-1", "role": "ORG_ADMIN"}])

ORG_DTO = {
    "id": "org-1",
    "name": "globo",
    "displayName": "Globo Corp",
    "active": True,
    "createdAt": "2026-01-01T00:00:00Z",
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
    store.set_cached_token(ident.id, CALLER_JWT, now, now + timedelta(hours=1))
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
    assert create.calls.last.request.headers["authorization"] == f"Bearer {CALLER_JWT}"
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



# --------------------------------------------------------------------------- #
# start / stop / reset: ORG_ADMIN of the org or PLATFORM_ADMIN, checked first,
# then passed down to every node command (vl-authorization-spec.md §3.5)
# --------------------------------------------------------------------------- #

from types import SimpleNamespace
from typing import Any

import pytest

from _helpers import add_service_account, add_user
from vl.commands import org as org_cmd

WIDE = {"COLUMNS": "200"}


class NodeCalls:
    """Records the node commands org start/stop/reset make, and the DOWN polls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, dict[str, Any]]] = []
        self.polled: list[int] = []

    def recorder(self, verb: str) -> Any:
        def call(org_name: str, n: int, **kwargs: Any) -> None:
            self.calls.append((verb, n, kwargs))
        return call


@pytest.fixture
def node_calls(monkeypatch: pytest.MonkeyPatch) -> NodeCalls:
    nc = NodeCalls()
    for verb in ("start", "stop", "reset"):
        monkeypatch.setattr(org_cmd.node, verb, nc.recorder(verb))

    def node_is_up(environment: store.Environment, node_: store.Node) -> bool:
        nc.polled.append(node_.org_node)  # as the node's own service account, not the operator
        return False

    monkeypatch.setattr(org_cmd.node, "node_is_up", node_is_up)
    monkeypatch.setattr(org_cmd.time, "sleep", lambda s: None)
    return nc


def _globo_with_nodes(count: int = 2) -> None:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "2026-01-01T00:00:00Z")
    store.upsert_organization("local", "acme", "org-2", "Acme", True, "2026-01-01T00:00:00Z")
    for n in range(1, count + 1):
        sa = store.add_identity("local", "SERVICE_ACCOUNT", f"sa-{n}", f"globo-{n}", f"globo-{n}")
        store.set_svc_acct(sa.id, "local", "globo", f"client-{n}", "secret", role="NODE")
        store.add_node("local", "globo", n, sa.id, 7000 + n)


ORG_COMMANDS = {
    "start": ["org", "start", "globo"],
    "stop": ["org", "stop", "globo"],
    "reset": ["org", "reset", "globo", "--yes"],
}
NODE_VERBS = {"start": {"start"}, "stop": {"stop"}, "reset": {"stop", "reset"}}


@pytest.mark.parametrize("command", list(ORG_COMMANDS))
@pytest.mark.parametrize(
    "orgs",
    [[{"orgId": "org-1", "role": "ORG_ADMIN"}], [{"orgId": "org-2", "role": "PLATFORM_ADMIN"}]],
    ids=["org-admin", "platform-admin"],
)
def test_org_command_is_allowed_and_passes_the_identity_to_every_node_call(
    command: str, orgs: list, node_calls: NodeCalls
) -> None:
    _globo_with_nodes()
    add_user("ops", orgs)

    result = runner.invoke(app, ORG_COMMANDS[command], env=WIDE)

    assert result.exit_code == 0, result.output
    assert {verb for verb, _, _ in node_calls.calls} == NODE_VERBS[command]
    assert {n for _, n, _ in node_calls.calls} == {1, 2}
    assert all(kw["as_"] == "ops" and kw["env"] == "local" for _, _, kw in node_calls.calls)


def test_org_command_passes_the_as_identity_not_the_default(node_calls: NodeCalls) -> None:
    _globo_with_nodes()
    add_user("ops", [{"orgId": "org-1", "role": "USER"}])
    add_user("boss", [{"orgId": "org-1", "role": "ORG_ADMIN"}], default=False)

    result = runner.invoke(app, [*ORG_COMMANDS["stop"], "--as", "boss"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert [kw["as_"] for _, _, kw in node_calls.calls] == ["boss", "boss"]


def test_org_reset_passes_include_snapshots_down(node_calls: NodeCalls) -> None:
    _globo_with_nodes()
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])

    runner.invoke(app, [*ORG_COMMANDS["reset"], "--include-snapshots"], env=WIDE)

    resets = [kw for verb, _, kw in node_calls.calls if verb == "reset"]
    assert resets and all(kw["include_snapshots"] is True and kw["yes"] is True for kw in resets)


def test_org_stop_still_polls_as_each_nodes_own_account(node_calls: NodeCalls) -> None:
    _globo_with_nodes()
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])

    runner.invoke(app, ORG_COMMANDS["stop"], env=WIDE)

    assert node_calls.polled == [2, 1]  # descending, one DOWN poll per node


ORG_REFUSALS = {
    "user": [{"orgId": "org-1", "role": "USER"}],
    "key-reader": [{"orgId": "org-1", "role": "KEY_READER"}],
    "admin-of-another-org": [{"orgId": "org-2", "role": "ORG_ADMIN"}],
}


@pytest.mark.parametrize("command", list(ORG_COMMANDS))
@pytest.mark.parametrize("who", list(ORG_REFUSALS))
def test_org_command_refusal_calls_no_node_command(command: str, who: str, node_calls: NodeCalls) -> None:
    _globo_with_nodes()
    add_user("ops", ORG_REFUSALS[who])

    result = runner.invoke(app, ORG_COMMANDS[command], env=WIDE)

    assert result.exit_code == 1
    assert "'ops' is not an admin of 'globo'" in result.output
    assert node_calls.calls == [] and node_calls.polled == []


@pytest.mark.parametrize("command", list(ORG_COMMANDS))
@pytest.mark.parametrize("role", ["NODE", "SYSTEM"])
def test_org_command_is_refused_for_a_service_account(command: str, role: str, node_calls: NodeCalls) -> None:
    _globo_with_nodes()
    add_service_account("ops-sa", "globo", role)

    result = runner.invoke(app, [*ORG_COMMANDS[command], "--as", "ops-sa"], env=WIDE)

    assert result.exit_code == 1
    assert "service accounts can't run this command" in result.output
    assert node_calls.calls == []


@pytest.mark.parametrize("command", list(ORG_COMMANDS))
def test_org_command_without_an_identity_is_refused(command: str, node_calls: NodeCalls) -> None:
    _globo_with_nodes()

    result = runner.invoke(app, ORG_COMMANDS[command], env=WIDE)

    assert result.exit_code == 1
    assert "No identity selected" in result.output
    assert node_calls.calls == []


def test_org_command_on_an_uncached_org_keeps_the_existing_error(node_calls: NodeCalls) -> None:
    _globo_with_nodes()
    add_user("ops", [{"orgId": "org-1", "role": "PLATFORM_ADMIN"}])

    result = runner.invoke(app, ["org", "start", "zeta"], env=WIDE)

    assert result.exit_code == 1
    assert "vl org show zeta" in result.output
    assert node_calls.calls == []


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(org_cmd, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True)))


def test_org_reset_refusal_never_shows_the_prompt(tty: None, node_calls: NodeCalls) -> None:
    _globo_with_nodes()
    add_user("ops", [{"orgId": "org-1", "role": "USER"}])

    result = runner.invoke(app, ["org", "reset", "globo"], input="y\n", env=WIDE)

    assert result.exit_code == 1
    assert "Are you sure" not in result.output
    assert node_calls.calls == []


def test_org_reset_prompt_still_works_after_the_check(tty: None, node_calls: NodeCalls) -> None:
    _globo_with_nodes()
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])

    declined = runner.invoke(app, ["org", "reset", "globo"], input="n\n", env=WIDE)
    assert declined.exit_code == 0 and "Are you sure" in declined.output
    assert node_calls.calls == []

    confirmed = runner.invoke(app, ["org", "reset", "globo"], input="y\n", env=WIDE)
    assert confirmed.exit_code == 0, confirmed.output
    assert {verb for verb, _, _ in node_calls.calls} == {"stop", "reset"}
