"""Local or remote (vl-agent-cli-spec.md §2) and the remote verbs (§7.1, §7.2).

The control plane is a respx stub; the node operations are recorders, so a test
can see whether the local loop ran. This host's address is the environment's
node_host.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from _helpers import add_service_account, add_user
from vl.app import app
from vl.lib import nodeops, store
from vl.lib.nodeops import NodeResult, Outcome

runner = CliRunner()
CP = "http://localhost:8082"
CP_NODES = f"{CP}/v1/nodes"
HERE = "192.168.8.2"
THERE = "192.168.8.9"
WIDE = {"COLUMNS": "200"}
CID = "7f3a1c2e-5b6d-7a10-8e21-3c9d4f5a6b7c"
PA = [{"orgId": "org-1", "role": "PLATFORM_ADMIN"}]
GLOBO_ADMIN = [{"orgId": "org-1", "role": "ORG_ADMIN"}]


@pytest.fixture(autouse=True)
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "t")
    store.upsert_organization("local", "acme", "org-2", "Acme", True, "t")
    store.update_environment("local", node_host=HERE)
    ran: dict[str, list[Any]] = {"start": [], "stop": [], "reset": []}

    def start_node(environment: Any, org: str, n: int, emit: Any, **_: Any) -> NodeResult:
        ran["start"].append((org, n))
        return NodeResult(Outcome.STARTED, "pid 1", pid=1)

    def stop_nodes_to_down(environment: Any, nodes: list[store.Node], emit: Any, **_: Any) -> list[Any]:
        ran["stop"].extend((n.org_name, n.org_node) for n in nodes)
        return [(n, NodeResult(Outcome.STOPPED, "pid 1")) for n in nodes]

    def reset_node(environment: Any, org: str, n: int, emit: Any, **_: Any) -> NodeResult:
        ran["reset"].append((org, n))
        return NodeResult(Outcome.RESET, "deleted")

    monkeypatch.setattr(nodeops, "start_node", start_node)
    monkeypatch.setattr(nodeops, "stop_nodes_to_down", stop_nodes_to_down)
    monkeypatch.setattr(nodeops, "reset_node", reset_node)
    monkeypatch.setattr(nodeops, "fetch_node_by_id", lambda env, node: {"state": "DOWN"})
    monkeypatch.setattr(nodeops, "node_is_up", lambda env, node: False)
    return ran


def node(org: str, n: int, *, state: str = "STOPPED") -> store.Node:
    sa = add_service_account(f"{org}-{n}", org)
    row = store.add_node("local", org, n, sa.id, 7000 + n + (100 if org == "acme" else 0))
    if state == "STOPPED":
        row = store.set_node_stopped(store.set_pid(row, 1))
    return row


def cp_row(org_id: str, n: int, host_: str = HERE, state: str = "DOWN") -> dict[str, Any]:
    return {"id": f"{org_id}-{n}", "orgId": org_id, "orgNode": n, "state": state, "host": host_, "port": 7000 + n}


def listing(*rows: dict[str, Any]) -> respx.Route:
    return respx.get(CP_NODES).mock(return_value=httpx.Response(200, json={"items": list(rows), "nextCursor": None}))


def accepted(targets: list[tuple[str, int, str]], skipped: list[tuple[str, int]] = (),  # type: ignore[assignment]
             cid: str = CID) -> httpx.Response:
    return httpx.Response(202, json={
        "correlationId": cid, "expiresAt": "2026-11-14T12:00:30Z", "waitUntil": "2026-11-14T12:01:00Z",
        "targets": [{"agentId": a, "agentName": a, "orgId": o, "orgNode": n} for o, n, a in targets],
        "skipped": [{"orgId": o, "orgNode": n, "reason": "NO_AGENT"} for o, n in skipped],
    })


def invoke(*args: str) -> Any:
    return runner.invoke(app, list(args), env=WIDE)


# --- local or remote (§2) ------------------------------------------------------------


@respx.mock
def test_a_scope_that_is_all_here_runs_the_local_loop_and_issues_nothing(world: dict[str, list[Any]]) -> None:
    add_user("ops", GLOBO_ADMIN)
    node("globo", 1), node("globo", 2)
    listing(cp_row("org-1", 1), cp_row("org-1", 2))
    issue = respx.post(f"{CP}/v1/orgs/org-1/commands")
    result = invoke("org", "stop", "globo")
    assert result.exit_code == 0, result.output
    assert world["stop"] == [("globo", 1), ("globo", 2)] and issue.call_count == 0
    assert store.list_issued_commands("local") == []


@respx.mock
def test_one_remote_node_sends_the_whole_verb_and_says_why_once(world: dict[str, list[Any]]) -> None:
    add_user("ops", GLOBO_ADMIN)
    node("globo", 1)
    listing(cp_row("org-1", 1), cp_row("org-1", 4, THERE), cp_row("org-1", 5, THERE))
    issue = respx.post(f"{CP}/v1/orgs/org-1/commands").mock(return_value=accepted(
        [("org-1", 5, "lab-2"), ("org-1", 4, "lab-2"), ("org-1", 1, "dev-box")]))
    result = invoke("org", "stop", "globo", "--no-wait")
    assert result.exit_code == 0, result.output
    assert json.loads(issue.calls.last.request.content) == {"type": "STOP", "command": "vl org stop"}
    assert world["stop"] == []  # the local node too goes through the control plane
    assert result.output.count("Not every node of org globo is on this machine (remote: globo/4, globo/5)") == 1
    assert f"Org globo stop published to 2 agents (3 nodes). Command {CID}" in result.output


@respx.mock
def test_local_forces_the_local_loop_without_asking_the_control_plane(world: dict[str, list[Any]]) -> None:
    add_user("ops", GLOBO_ADMIN)
    node("globo", 1)
    nodes = listing(cp_row("org-1", 4, THERE))
    result = invoke("org", "start", "globo", "--local")
    assert result.exit_code == 0 and world["start"] == [("globo", 1)] and nodes.call_count == 0


@respx.mock
def test_remote_forces_the_control_plane_even_when_everything_is_here(world: dict[str, list[Any]]) -> None:
    add_user("ops", GLOBO_ADMIN)
    node("globo", 1)
    nodes = listing(cp_row("org-1", 1))
    issue = respx.post(f"{CP}/v1/orgs/org-1/commands").mock(return_value=accepted([("org-1", 1, "dev-box")]))
    result = invoke("org", "start", "globo", "--remote", "--no-wait")
    assert result.exit_code == 0, result.output
    assert issue.call_count == 1 and world["start"] == [] and nodes.call_count == 0
    assert "--remote: sending the command through the control plane." in result.output


def test_local_and_remote_together_is_a_usage_error() -> None:
    add_user("ops", GLOBO_ADMIN)
    assert invoke("org", "stop", "globo", "--local", "--remote").exit_code == 2


@pytest.mark.parametrize("failure", [httpx.ConnectError("refused"), httpx.Response(502)])
@respx.mock
def test_an_unreachable_control_plane_aborts_with_4_and_changes_nothing(failure: Any,
                                                                       world: dict[str, list[Any]]) -> None:
    add_user("ops", GLOBO_ADMIN)
    node("globo", 1)
    route = respx.get(CP_NODES)
    route.mock(return_value=failure) if isinstance(failure, httpx.Response) else route.mock(side_effect=failure)
    result = invoke("org", "stop", "globo")
    assert result.exit_code == 4
    assert "Cannot reach the control plane to find the nodes in scope; nothing was done. Use --local" in \
        " ".join(result.output.split())
    assert world["stop"] == [] and store.list_issued_commands("local") == []


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
def test_a_refused_listing_aborts_with_the_control_planes_error(status: int, world: dict[str, list[Any]]) -> None:
    add_user("ops", GLOBO_ADMIN)
    node("globo", 1)
    respx.get(CP_NODES).mock(return_value=httpx.Response(status, json={"detail": "Not for you"}))
    respx.post("http://localhost:8080/auth/user/login").mock(return_value=httpx.Response(401))
    result = invoke("org", "stop", "globo")
    assert result.exit_code == 1 and world["stop"] == []


@respx.mock
def test_a_stale_copied_row_makes_the_scope_remote(world: dict[str, list[Any]]) -> None:
    add_user("ops", GLOBO_ADMIN)
    node("globo", 1)
    listing(cp_row("org-1", 1, THERE))  # registered from the other host
    respx.post(f"{CP}/v1/orgs/org-1/commands").mock(return_value=accepted([("org-1", 1, "lab-2")]))
    result = invoke("org", "start", "globo", "--no-wait")
    assert result.exit_code == 0 and world["start"] == []
    assert "remote: globo/1" in result.output


@respx.mock
def test_a_new_node_never_makes_a_scope_remote(world: dict[str, list[Any]]) -> None:
    add_user("ops", GLOBO_ADMIN)
    node("globo", 1), node("globo", 2, state="NEW")
    listing(cp_row("org-1", 1))
    result = invoke("org", "start", "globo")
    assert result.exit_code == 0 and world["start"] == [("globo", 1), ("globo", 2)]


@respx.mock
def test_a_node_in_the_local_store_is_local_without_any_lookup(world: dict[str, list[Any]]) -> None:
    add_user("ops", GLOBO_ADMIN)
    node("globo", 1)
    nodes = listing(cp_row("org-1", 1, THERE))
    result = invoke("node", "start", "globo", "1")
    assert result.exit_code == 0 and world["start"] == [("globo", 1)] and nodes.call_count == 0


@respx.mock
def test_a_node_not_in_the_store_is_a_node_command(world: dict[str, list[Any]]) -> None:
    add_user("ops", GLOBO_ADMIN)
    issue = respx.post(f"{CP}/v1/orgs/org-1/nodes/4/commands").mock(
        return_value=accepted([("org-1", 4, "lab-2")]))
    result = invoke("node", "stop", "globo", "4", "--no-wait")
    assert result.exit_code == 0, result.output
    assert json.loads(issue.calls.last.request.content) == {"type": "STOP", "command": "vl node stop"}
    assert "Node globo/4 is not on this machine" in result.output
    assert f"Node globo/4 stop published to 1 agent (1 node). Command {CID}" in result.output
    [rec] = store.list_issued_commands("local")
    assert (rec.correlation_id, rec.command_type, rec.scope, rec.org_name, rec.org_node) == (
        CID, "STOP", "NODE", "globo", 4)


@respx.mock
def test_node_remote_forces_a_local_node_through_the_control_plane(world: dict[str, list[Any]]) -> None:
    add_user("ops", GLOBO_ADMIN)
    node("globo", 1)
    issue = respx.post(f"{CP}/v1/orgs/org-1/nodes/1/commands").mock(return_value=accepted([("org-1", 1, "a")]))
    result = invoke("node", "start", "globo", "1", "--remote", "--no-wait")
    assert result.exit_code == 0 and issue.call_count == 1 and world["start"] == []


# --- the remote verbs (§7.1, §7.2) ---------------------------------------------------------


@respx.mock
def test_the_role_check_comes_first(world: dict[str, list[Any]]) -> None:
    add_user("ops", [{"orgId": "org-1", "role": "USER"}])
    nodes = listing(cp_row("org-1", 4, THERE))
    issue = respx.post(f"{CP}/v1/orgs/org-1/commands")
    result = invoke("org", "stop", "globo", "--remote")
    assert result.exit_code == 1 and "not an admin" in result.output
    assert nodes.call_count == 0 and issue.call_count == 0


@respx.mock
def test_a_network_command_needs_a_platform_admin_user_not_a_service_account() -> None:
    add_service_account("robot", "globo", role="SYSTEM")
    issue = respx.post(f"{CP}/v1/network/commands")
    result = invoke("network", "stop", "--remote", "--as", "robot")
    assert result.exit_code == 1 and "service account" in result.output and issue.call_count == 0


@respx.mock
def test_network_stop_request_and_output_with_skipped_new_and_a_stopped_agent(
        world: dict[str, list[Any]]) -> None:
    add_user("ops", PA)
    node("globo", 3), node("globo", 9, state="NEW")
    sa = add_service_account("globo-agent", "globo", role="NODE_AGENT")
    store.add_agent("local", "globo", sa.id)  # NEW: not running
    listing(cp_row("org-1", 3), cp_row("org-1", 4, THERE), cp_row("org-1", 7, THERE), cp_row("org-1", 8, THERE))
    issue = respx.post(f"{CP}/v1/network/commands").mock(return_value=accepted(
        [("org-1", 4, "lab-2"), ("org-1", 3, "dev-box")], skipped=[("org-1", 7), ("org-1", 8)]))

    result = invoke("network", "stop", "--no-wait")

    assert result.exit_code == 0, result.output
    assert json.loads(issue.calls.last.request.content) == {"type": "STOP", "command": "vl network stop"}
    out = result.output
    assert f"Network stop published to 2 agents (2 nodes). Command {CID}" in out
    assert "  Skipped (no agent): globo/7, globo/8" in out
    assert "  Never started on this machine, so not reachable by remote commands: globo/9" in out
    assert ("Warning: the agent for org globo on this host is not running; its nodes will not answer. "
            "Start it with vl agent start globo.") in " ".join(out.split())
    assert "Look at the result: vl command show --last   Wait for it: vl command watch --last" in out
    [rec] = store.list_issued_commands("local")
    assert (rec.scope, rec.org_name, rec.org_node, rec.expires_at) == ("NETWORK", None, None, "2026-11-14T12:00:30Z")


@respx.mock
def test_org_reset_remote_sends_include_snapshots_and_needs_yes_without_a_terminal() -> None:
    add_user("ops", GLOBO_ADMIN)
    issue = respx.post(f"{CP}/v1/orgs/org-1/commands").mock(return_value=accepted([("org-1", 4, "lab-2")]))
    refused = invoke("org", "reset", "globo", "--remote")
    assert refused.exit_code == 1 and "--yes" in refused.output and issue.call_count == 0
    result = invoke("org", "reset", "globo", "--remote", "--yes", "--include-snapshots", "--no-wait")
    assert result.exit_code == 0, result.output
    assert json.loads(issue.calls.last.request.content) == {
        "type": "RESET", "command": "vl org reset", "options": {"includeSnapshots": True}}
    plain = invoke("org", "reset", "globo", "--remote", "--yes", "--no-wait")
    assert "options" not in json.loads(issue.calls.last.request.content) and plain.exit_code == 0


@respx.mock
def test_no_targets_exits_1_with_the_control_planes_message() -> None:
    add_user("ops", GLOBO_ADMIN)
    respx.post(f"{CP}/v1/orgs/org-1/commands").mock(return_value=httpx.Response(409, json={
        "detail": "No node in scope has an agent: nothing was published.", "errorCode": "CONFLICT",
        "context": {"reason": "NO_TARGETS", "skipped": [{"orgId": "org-1", "orgNode": 4, "reason": "NO_AGENT"}]}}))
    result = invoke("org", "start", "globo", "--remote")
    assert result.exit_code == 1
    assert "No node in scope has an agent: nothing was published." in result.output
    assert "No agent for: globo/4" in result.output
    assert store.list_issued_commands("local") == []


@respx.mock
def test_a_refused_reset_exits_1_naming_each_node() -> None:
    add_user("ops", GLOBO_ADMIN)
    respx.post(f"{CP}/v1/orgs/org-1/commands").mock(return_value=httpx.Response(409, json={
        "detail": "Every node the command is for must be DOWN before it can be reset.", "errorCode": "CONFLICT",
        "context": {"reason": "NOT_DOWN", "nodes": [
            {"orgId": "org-1", "orgName": "globo", "orgNode": 4, "state": "UP"},
            {"orgId": "org-1", "orgName": "globo", "orgNode": 5, "state": "STARTING"}]}}))
    result = invoke("org", "reset", "globo", "--remote", "--yes")
    assert result.exit_code == 1
    assert "Not down: globo/4 (UP), globo/5 (STARTING)" in " ".join(result.output.split())


@pytest.mark.parametrize("status", [401, 403, 404])
@respx.mock
def test_a_refusal_of_the_command_exits_1(status: int) -> None:
    add_user("ops", GLOBO_ADMIN)
    respx.post(f"{CP}/v1/orgs/org-1/commands").mock(return_value=httpx.Response(status, json={"detail": "no"}))
    respx.post("http://localhost:8080/auth/user/login").mock(return_value=httpx.Response(401))
    assert invoke("org", "stop", "globo", "--remote").exit_code == 1


@respx.mock
def test_no_wait_is_ignored_when_the_verb_runs_locally(world: dict[str, list[Any]]) -> None:
    add_user("ops", GLOBO_ADMIN)
    node("globo", 1)
    listing(cp_row("org-1", 1))
    result = invoke("org", "stop", "globo", "--no-wait")
    assert result.exit_code == 0 and world["stop"] == [("globo", 1)]
    assert "vl command show" not in result.output


# --- vl network reset when some node is remote (§7.3) ---------------------------------------

RESET = f"{CP}/v1/network/reset"


@respx.mock
def test_network_reset_resets_the_control_plane_then_issues_one_network_reset_and_waits(
        world: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    from vl.commands import wait as wait_mod
    monkeypatch.setattr(wait_mod, "_sleep", lambda s: None)
    add_user("ops", PA)
    node("globo", 1)
    listing(cp_row("org-1", 1), cp_row("org-1", 4, THERE))
    order: list[str] = []
    respx.post(RESET).mock(side_effect=lambda r: (order.append("cp-reset"),
                                                  httpx.Response(200, json={"deleted": {}}))[1])
    issue = respx.post(f"{CP}/v1/network/commands").mock(side_effect=lambda r: (
        order.append("command"), accepted([("org-1", 1, "dev-box"), ("org-1", 4, "lab-2")]))[1])
    respx.get(f"{CP}/v1/commands/{CID}").mock(return_value=httpx.Response(200, json={
        "correlationId": CID, "status": "SUCCEEDED",
        "counts": {"targets": 2, "done": 2, "failed": 0, "noAnswer": 0, "skipped": 0, "pending": 0},
        "targets": [{"orgId": "org-1", "orgNode": n, "state": "DONE", "outcome": "RESET", "detail": "deleted",
                     "completedAt": "2026-11-14T12:00:01Z"} for n in (1, 4)]}))

    result = invoke("network", "reset", "--yes")

    assert result.exit_code == 0, result.output
    assert order == ["cp-reset", "command"]
    assert json.loads(issue.calls.last.request.content) == {
        "type": "RESET", "command": "vl network reset", "options": {"includeSnapshots": True}}
    assert world["reset"] == []  # no local loop: the local node is the agent's
    assert "2 of 2 succeeded." in result.output
    [rec] = store.list_issued_commands("local")
    assert (rec.command_type, rec.scope) == ("RESET", "NETWORK")


@respx.mock
def test_network_reset_refused_by_the_control_plane_issues_no_command() -> None:
    add_user("ops", PA)
    node("globo", 1)
    listing(cp_row("org-1", 4, THERE))
    respx.post(RESET).mock(return_value=httpx.Response(409, json={"detail": "not down", "context": {
        "nodes": [{"orgName": "globo", "orgNode": 4, "state": "UP"}]}}))
    issue = respx.post(f"{CP}/v1/network/commands")
    result = invoke("network", "reset", "--yes")
    assert result.exit_code == 1 and "globo/node4 (UP)" in result.output and issue.call_count == 0


@respx.mock
def test_a_409_on_the_reset_command_names_the_nodes() -> None:
    add_user("ops", PA)
    listing(cp_row("org-1", 4, THERE))
    respx.post(RESET).mock(return_value=httpx.Response(200, json={"deleted": {}}))
    respx.post(f"{CP}/v1/network/commands").mock(return_value=httpx.Response(409, json={
        "detail": "must be DOWN", "context": {"reason": "NOT_DOWN", "nodes": [
            {"orgId": "org-1", "orgName": "globo", "orgNode": 4, "state": "UP"}]}}))
    result = invoke("network", "reset", "--yes")
    assert result.exit_code == 1 and "Not down: globo/4 (UP)" in " ".join(result.output.split())


@respx.mock
def test_network_reset_remote_needs_a_platform_admin_before_anything_changes() -> None:
    add_user("ops", GLOBO_ADMIN)
    reset = respx.post(RESET)
    result = invoke("network", "reset", "--yes", "--remote")
    assert result.exit_code == 1 and "PLATFORM_ADMIN" in result.output and reset.call_count == 0


@respx.mock
def test_network_reset_with_everything_here_is_the_local_form(world: dict[str, list[Any]]) -> None:
    add_user("ops", PA)
    node("globo", 1)
    listing(cp_row("org-1", 1))
    respx.post(RESET).mock(return_value=httpx.Response(200, json={"deleted": {}}))
    issue = respx.post(f"{CP}/v1/network/commands")
    result = invoke("network", "reset", "--yes")
    assert result.exit_code == 0, result.output
    assert issue.call_count == 0 and world["reset"] == [("globo", 1)]
