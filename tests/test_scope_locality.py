"""Which of this host's nodes are local when their registration names another host
(vl-agent-cli-spec.md §2, scope.locality).

A node with a row here is remote only when all three hold: the control plane has it
registered from another host, its row has no live process here, and the control
plane's state for it is not DOWN. The control plane is a respx stub; the live-process
check is stubbed by pid.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import respx

from _helpers import add_user
from test_remote_verbs import PA, accepted, cp_row, invoke, listing, node, runner, world  # noqa: F401
from vl.app import app
from vl.commands import scope as scope_mod
from vl.lib import nodeops, store

NEW_HOST = "100.106.199.60"
OLD_HOST = "192.168.8.105"
CP = "http://localhost:8082"
ALIVE_PID = 4242


@pytest.fixture(autouse=True)
def laptop(world: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    store.update_environment("local", node_host=NEW_HOST)
    # A row has a live process here exactly when its pid is ALIVE_PID.
    monkeypatch.setattr(nodeops, "is_our_process", lambda n: n.pid == ALIVE_PID)


def running(org: str, n: int) -> store.Node:
    return store.set_pid(node(org, n, state="NEW"), ALIVE_PID)


def resolve(org: str | None = None) -> scope_mod.Scope:
    env = store.get_environment("local")
    caller = store.resolve_identity("local", None)
    organization = store.get_organization("local", org) if org else None
    return scope_mod.resolve_scope(env, caller, organization=organization)


def where(s: scope_mod.Scope) -> list[tuple[str, bool, bool]]:
    return [(n.label, n.local, n.moved) for n in s.nodes]


# --- the three conditions, one at a time ---------------------------------------------------


@pytest.mark.parametrize("registered, alive, state, local", [
    (OLD_HOST, False, "UP", False),          # all three hold: a copied row, remote
    (NEW_HOST, False, "UP", True),           # (1) fails: registered from this host
    (OLD_HOST, True, "UP", True),            # (2) fails: a live process here
    (OLD_HOST, False, "DOWN", True),         # (3) fails: the control plane has it DOWN
    (OLD_HOST, False, "STANDBY", False),     # STANDBY may be running
    (OLD_HOST, False, None, False),          # a state it does not name may be running
])
@respx.mock
def test_each_condition_alone_keeps_a_row_local(registered: str, alive: bool, state: str | None,
                                                local: bool) -> None:
    add_user("ops", PA)
    running("globo", 1) if alive else node("globo", 1)
    listing({**cp_row("org-1", 1, registered), "state": state})
    [n] = resolve("globo").nodes
    assert n.local is local
    assert n.moved is (local and registered != NEW_HOST)


@respx.mock
def test_no_registration_is_local_and_not_moved() -> None:
    add_user("ops", PA)
    node("globo", 1)
    listing()
    assert where(resolve("globo")) == [("globo/1", True, False)]


@respx.mock
def test_a_node_with_no_row_stays_remote_whatever_its_state() -> None:
    add_user("ops", PA)
    listing(cp_row("org-1", 4, OLD_HOST, "DOWN"))
    assert where(resolve("globo")) == [("globo/4", False, False)]


# --- the laptop's cases ---------------------------------------------------------------------


@respx.mock
def test_the_laptops_running_nodes_registered_from_the_old_address_are_stopped_here(
        world: dict[str, list[Any]]) -> None:  # noqa: F811
    add_user("ops", PA)
    for n in (1, 2):
        running("globo", n)
    running("acme", 1)
    listing(cp_row("org-1", 1, OLD_HOST, "UP"), cp_row("org-1", 2, OLD_HOST, "UP"),
            cp_row("org-2", 1, OLD_HOST, "UP"))
    issue = respx.post(f"{CP}/v1/network/commands")
    result = invoke("network", "stop")
    assert result.exit_code == 0, result.output
    assert sorted(world["stop"]) == [("acme", 1), ("globo", 1), ("globo", 2)] and issue.call_count == 0
    out = " ".join(result.output.split())
    line = (f"acme/1, globo/1, globo/2 last registered from {OLD_HOST}; this host is {NEW_HOST}: "
            f"treated as local, they re-register when started.")
    assert out.count(line) == 1
    assert "sending the command through the control plane" not in out


@respx.mock
def test_the_laptops_stopped_nodes_with_the_old_address_and_down_are_started_here(
        world: dict[str, list[Any]]) -> None:  # noqa: F811
    add_user("ops", PA)
    node("globo", 1), node("globo", 2)
    listing(cp_row("org-1", 1, OLD_HOST, "DOWN"), cp_row("org-1", 2, OLD_HOST, "DOWN"))
    issue = respx.post(f"{CP}/v1/network/commands")
    result = invoke("network", "start")
    assert result.exit_code == 0, result.output
    assert sorted(world["start"]) == [("globo", 1), ("globo", 2)] and issue.call_count == 0
    out = " ".join(result.output.split())
    assert out.count(f"globo/1, globo/2 last registered from {OLD_HOST}; this host is {NEW_HOST}") == 1


@respx.mock
def test_a_copied_row_up_elsewhere_with_no_process_here_is_remote_as_before(
        world: dict[str, list[Any]]) -> None:  # noqa: F811
    add_user("ops", PA)
    node("globo", 1)
    listing(cp_row("org-1", 1, OLD_HOST, "UP"))
    respx.post(f"{CP}/v1/orgs/org-1/commands").mock(return_value=accepted([("org-1", 1, "lab-2")]))
    result = invoke("org", "start", "globo", "--no-wait")
    assert result.exit_code == 0, result.output
    assert world["start"] == [] and "remote: globo/1" in result.output
    assert "treated as local" not in result.output


@respx.mock
def test_the_note_names_each_old_host_and_only_the_moved_nodes() -> None:
    add_user("ops", PA)
    running("globo", 1), node("globo", 2), node("globo", 3)
    listing(cp_row("org-1", 1, OLD_HOST, "UP"), cp_row("org-1", 2, " 10.0.0.7 ", "DOWN"),
            cp_row("org-1", 3, NEW_HOST, "UP"))
    s = resolve("globo")
    assert [n.label for n in s.moved] == ["globo/1", "globo/2"]
    assert s.moved_note() == (f"globo/1 last registered from {OLD_HOST}; globo/2 last registered from "
                              f"10.0.0.7; this host is {NEW_HOST}: treated as local, they re-register "
                              f"when started.")


@respx.mock
def test_no_note_when_nothing_moved() -> None:
    add_user("ops", PA)
    node("globo", 1)
    listing(cp_row("org-1", 1, NEW_HOST, "UP"))
    assert resolve("globo").moved_note() is None


# --- vl node list --all agrees ----------------------------------------------------------------


@respx.mock
def test_list_all_where_column_is_resolve_scopes_answer() -> None:
    add_user("ops", PA)
    me = store.resolve_identity("local", None).id
    for org in ("globo", "acme"):
        store.upsert_org_membership(me, "local", org, "ORG_ADMIN")
    running("globo", 1)                      # old host, alive: local
    node("globo", 2)                         # old host, DOWN: local
    node("globo", 3)                         # old host, UP, not alive: remote (copied row)
    node("acme", 1)                          # this host: local
    listing(cp_row("org-1", 1, OLD_HOST, "UP"), cp_row("org-1", 2, OLD_HOST, "DOWN"),
            cp_row("org-1", 3, OLD_HOST, "UP"), cp_row("org-1", 4, OLD_HOST, "UP"),
            cp_row("org-2", 1, NEW_HOST, "UP"))
    result = runner.invoke(app, ["node", "list", "--all"], env={"VL_OUTPUT": "json", "COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    resolved = resolve()
    assert [(r["org"], r["node"], r["where"] == "local") for r in rows] == [
        (n.org_name, n.org_node, n.local) for n in resolved.nodes]
    assert [(r["org"], r["node"], r["where"]) for r in rows] == [
        ("acme", 1, "local"), ("globo", 1, "local"), ("globo", 2, "local"),
        ("globo", 3, "remote (copied row)"), ("globo", 4, "remote")]


@respx.mock
def test_list_all_prints_the_note_in_table_mode() -> None:
    add_user("ops", PA)
    store.upsert_org_membership(store.resolve_identity("local", None).id, "local", "globo", "ORG_ADMIN")
    node("globo", 1)
    listing(cp_row("org-1", 1, OLD_HOST, "DOWN"))
    result = runner.invoke(app, ["node", "list", "--all"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert out.count(f"globo/1 last registered from {OLD_HOST}; this host is {NEW_HOST}") == 1
