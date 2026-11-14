"""Scope resolution (vl-agent-cli-spec.md §2) and `vl node list --remote|--all` (§2.1).

The control plane is a respx stub. "This host's address" is the environment's
node_host where a test sets one, a stubbed `--print-host`, or stubbed interfaces.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from _helpers import add_service_account, add_user
from vl.agent import hostcheck
from vl.app import app
from vl.commands import scope as scope_mod
from vl.lib import api, host, store

runner = CliRunner()
CP_NODES = "http://localhost:8082/v1/nodes"
HERE = "192.168.8.2"
THERE = "192.168.8.9"
JSON = {"VL_OUTPUT": "json", "COLUMNS": "200"}


def cp_row(org_id: str, n: int, *, host_: str | None = HERE, state: str = "UP") -> dict[str, Any]:
    return {"id": f"{org_id}-{n}", "orgId": org_id, "orgNode": n, "state": state, "host": host_,
            "port": 7000 + n, "metadataJson": None}


def cp(*rows: dict[str, Any], next_cursor: str | None = None) -> httpx.Response:
    return httpx.Response(200, json={"items": list(rows), "nextCursor": next_cursor})


@pytest.fixture(autouse=True)
def world(monkeypatch: pytest.MonkeyPatch) -> None:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "t")
    store.upsert_organization("local", "acme", "org-2", "Acme", True, "t")
    store.update_environment("local", node_host=HERE)
    # Nothing here may run a real verilock, or read the real interfaces.
    monkeypatch.setattr(host, "local_identities", lambda: {HERE, "127.0.0.1", "dev-box"})


def node(org: str, n: int, *, state: str = "STOPPED") -> store.Node:
    sa = add_service_account(f"{org}-{n}", org)
    row = store.add_node("local", org, n, sa.id, 7000 + n + (100 if org == "acme" else 0))
    if state == "STOPPED":
        row = store.set_node_stopped(store.set_pid(row, 1))
    return row


def resolve(org: str | None = None, org_node: int | None = None, **kw: Any) -> scope_mod.Scope:
    env = store.get_environment("local")
    caller = store.resolve_identity("local", None)
    organization = store.get_organization("local", org) if org else None
    return scope_mod.resolve_scope(env, caller, organization=organization, org_node=org_node, **kw)


def labels(nodes: list[scope_mod.ScopeNode]) -> list[str]:
    return [n.label for n in nodes]


# --- resolve_scope --------------------------------------------------------------------


@respx.mock
def test_a_scope_that_is_all_here_is_local() -> None:
    add_user()
    node("globo", 1), node("globo", 2)
    respx.get(CP_NODES).mock(return_value=cp(cp_row("org-1", 1), cp_row("org-1", 2)))
    s = resolve("globo")
    assert labels(s.local) == ["globo/1", "globo/2"] and s.remote == []


@respx.mock
def test_a_node_with_no_local_row_is_remote() -> None:
    add_user()
    node("globo", 1)
    respx.get(CP_NODES).mock(return_value=cp(cp_row("org-1", 1), cp_row("org-1", 4, host_=THERE)))
    s = resolve("globo")
    assert labels(s.local) == ["globo/1"] and labels(s.remote) == ["globo/4"]
    assert s.remote[0].row is None and s.remote[0].host == THERE
    assert s.why_remote() == ("Not every node of org globo is on this machine (remote: globo/4): "
                              "sending the command through the control plane.")


@respx.mock
def test_a_copied_row_of_a_node_registered_elsewhere_counts_as_remote() -> None:
    add_user()
    node("globo", 1)
    respx.get(CP_NODES).mock(return_value=cp(cp_row("org-1", 1, host_=THERE)))
    [n] = resolve("globo").nodes
    assert not n.local and n.row is not None


@respx.mock
def test_hosts_are_compared_trimmed_and_lower_cased() -> None:
    add_user()
    store.update_environment("local", node_host="Dev-Box.lan")
    node("globo", 1)
    respx.get(CP_NODES).mock(return_value=cp(cp_row("org-1", 1, host_=" dev-box.LAN ")))
    assert resolve("globo").nodes[0].local


@respx.mock
def test_a_new_node_never_makes_a_scope_remote() -> None:
    add_user()
    node("globo", 1), node("globo", 2, state="NEW")
    respx.get(CP_NODES).mock(return_value=cp(cp_row("org-1", 1)))  # the CP has never seen node 2
    s = resolve("globo")
    assert s.remote == [] and [n.is_new for n in s.local] == [False, True]


@respx.mock
def test_the_network_scope_takes_every_org_and_org_ids_it_has_never_cached() -> None:
    add_user("ops", [{"orgId": "org-1", "role": "PLATFORM_ADMIN"}])
    node("globo", 1), node("acme", 1)
    respx.get(CP_NODES).mock(return_value=cp(cp_row("org-1", 1), cp_row("org-2", 1),
                                             cp_row("org-9", 3, host_=THERE)))
    s = resolve()
    assert s.kind == "NETWORK" and labels(s.local) == ["acme/1", "globo/1"]
    assert labels(s.remote) == ["org-9/3"]  # an org this store never cached: named by its id
    assert "Not every node of the network" in s.why_remote()


@respx.mock
def test_a_node_scope_is_that_node_only() -> None:
    add_user()
    node("globo", 1)
    respx.get(CP_NODES).mock(return_value=cp(cp_row("org-1", 1), cp_row("org-1", 2, host_=THERE)))
    s = resolve("globo", 2)
    assert s.kind == "NODE" and labels(s.nodes) == ["globo/2"] and labels(s.remote) == ["globo/2"]


@respx.mock
def test_the_listing_is_paged_to_the_end() -> None:
    add_user()
    route = respx.get(CP_NODES)
    route.side_effect = [cp(cp_row("org-1", 1), next_cursor="c2"), cp(cp_row("org-1", 2, host_=THERE))]
    assert labels(resolve("globo").remote) == ["globo/1", "globo/2"]
    assert route.calls[1].request.url.params["cursor"] == "c2"


@pytest.mark.parametrize("failure", [httpx.ConnectError("refused"), httpx.Response(503)])
@respx.mock
def test_an_unreachable_control_plane_is_scope_unreachable(failure: Any) -> None:
    add_user()
    route = respx.get(CP_NODES)
    if isinstance(failure, httpx.Response):
        route.mock(return_value=failure)
    else:
        route.mock(side_effect=failure)
    with pytest.raises(scope_mod.ScopeUnreachable) as e:
        resolve("globo")
    assert "nothing was done. Use --local" in str(e.value)


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
def test_a_refusal_is_the_control_planes_error(status: int) -> None:
    add_user()
    respx.get(CP_NODES).mock(return_value=httpx.Response(status, json={"message": "nope"}))
    respx.post("http://localhost:8080/auth/user/login").mock(return_value=httpx.Response(401))
    with pytest.raises((api.ApiError, Exception)) as e:
        resolve("globo")
    assert not isinstance(e.value, scope_mod.ScopeUnreachable)


# --- this host's address ----------------------------------------------------------------


def _verilock(isolated_home: Path) -> Path:
    binary = isolated_home / ".veritaslock" / "bin" / "verilock"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    return binary


def test_this_host_is_the_node_host_first(isolated_home: Path) -> None:
    _verilock(isolated_home)
    here = scope_mod.this_host(store.get_environment("local"), print_host=lambda b: pytest.fail("ran"))
    assert (here.address, here.source) == (HERE, "node_host")


def test_this_host_is_print_host_without_a_node_host(isolated_home: Path) -> None:
    store.update_environment("local", clear_node_host=True)
    binary = _verilock(isolated_home)
    ran: list[Path] = []
    here = scope_mod.this_host(store.get_environment("local"),
                               print_host=lambda b: (ran.append(b), "10.0.0.7")[1])
    assert (here.address, here.source, ran) == ("10.0.0.7", "print-host", [binary])
    assert here.is_here("10.0.0.7") and not here.is_here(HERE)


def test_with_neither_the_interfaces_decide(isolated_home: Path) -> None:
    store.update_environment("local", clear_node_host=True)
    _verilock(isolated_home)

    def fails(b: Path) -> str:
        raise hostcheck.PrintHostError("no --print-host")

    here = scope_mod.this_host(store.get_environment("local"), print_host=fails)
    assert (here.address, here.source) == (None, "interfaces")
    assert here.is_here(HERE) and here.is_here("DEV-BOX") and not here.is_here(THERE)
    no_binary = scope_mod.this_host(store.get_environment("local"))
    assert no_binary.source == "interfaces"


def test_no_registered_host_is_never_a_mismatch() -> None:
    assert scope_mod.ThisHost(HERE, "node_host").is_here(None)
    assert scope_mod.ThisHost(HERE, "node_host").is_here("  ")


# --- vl node list --remote and --all ------------------------------------------------------


def listing(*args: str) -> Any:
    return runner.invoke(app, ["node", "list", *args], env=JSON)


@respx.mock
def test_list_remote_is_the_control_planes_nodes_and_nothing_local() -> None:
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])
    store.upsert_org_membership(store.resolve_identity("local", None).id, "local", "globo", "ORG_ADMIN")
    node("globo", 1), node("globo", 3, state="NEW")
    respx.get(CP_NODES).mock(return_value=cp(cp_row("org-1", 1), cp_row("org-1", 2, host_=THERE)))
    result = listing("--remote")
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert [(r["org"], r["node"], r["host"], r["local_row"]) for r in rows] == [
        ("globo", 1, HERE, "yes"), ("globo", 2, THERE, "no")]  # the NEW node 3 is local only


@respx.mock
def test_list_all_is_local_rows_and_remote_nodes_with_the_host_of_each() -> None:
    add_user()
    store.upsert_org_membership(store.resolve_identity("local", None).id, "local", "globo", "ORG_ADMIN")
    node("globo", 1), node("globo", 3, state="NEW"), node("globo", 5)
    respx.get(CP_NODES).mock(return_value=cp(cp_row("org-1", 1), cp_row("org-1", 2, host_=THERE),
                                             cp_row("org-1", 5, host_=THERE)))
    rows = json.loads(listing("--all").stdout)
    assert [(r["node"], r["where"], r["host"]) for r in rows] == [
        (1, "local", HERE), (2, "remote", THERE), (3, "local", HERE), (5, "remote (copied row)", THERE)]


@respx.mock
def test_list_all_for_an_org_is_the_scope_the_org_verbs_resolve() -> None:
    add_user()
    store.upsert_org_membership(store.resolve_identity("local", None).id, "local", "globo", "ORG_ADMIN")
    store.upsert_org_membership(store.resolve_identity("local", None).id, "local", "acme", "ORG_ADMIN")
    node("globo", 1), node("acme", 1)
    respx.get(CP_NODES).mock(return_value=cp(cp_row("org-1", 1), cp_row("org-1", 2, host_=THERE),
                                             cp_row("org-2", 1)))
    rows = json.loads(listing("--all", "--org", "globo").stdout)
    resolved = resolve("globo")
    assert [(r["org"], r["node"], r["where"] == "local") for r in rows] == [
        (n.org_name, n.org_node, n.local) for n in resolved.nodes]


@respx.mock
def test_list_all_with_the_control_plane_unreachable_warns_and_shows_the_local_rows() -> None:
    add_user()
    store.upsert_org_membership(store.resolve_identity("local", None).id, "local", "globo", "ORG_ADMIN")
    node("globo", 1)
    respx.get(CP_NODES).mock(side_effect=httpx.ConnectError("refused"))
    result = listing("--all")
    assert result.exit_code == 0
    assert "control plane unreachable" in result.output
    assert [r["node"] for r in json.loads(result.stdout)] == [1]


@respx.mock
def test_list_remote_with_the_control_plane_unreachable_is_an_error() -> None:
    add_user()
    respx.get(CP_NODES).mock(side_effect=httpx.ConnectError("refused"))
    result = listing("--remote")
    assert result.exit_code == 1 and "could not be reached" in result.output


@pytest.mark.parametrize("flag", ["--remote", "--all"])
@respx.mock
def test_without_an_identity_or_with_a_refusal_only_the_error_is_printed(flag: str) -> None:
    node("globo", 1)
    result = listing(flag)
    assert result.exit_code == 1 and result.stdout.lstrip().startswith("Error")
    add_user()
    respx.get(CP_NODES).mock(return_value=httpx.Response(403, json={"message": "Forbidden"}))
    refused = listing(flag)
    assert refused.exit_code == 1 and "globo" not in refused.stdout


def test_remote_and_all_together_is_a_usage_error() -> None:
    assert listing("--remote", "--all").exit_code == 2
