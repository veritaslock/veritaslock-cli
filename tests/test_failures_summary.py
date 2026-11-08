"""Failures are no longer swallowed (vl-agent-cli-spec.md §9).

vl org stop, vl network stop and vl network reset keep their per-node and
per-org warnings, then print one summary of every failure and exit 1. A node
that was already stopped is not a failure; neither is one already running for
the loops that start (their wording is unchanged and tested elsewhere). The
node-level operations are faked in vl.lib.nodeops; the org and network loops
are the real ones.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from _helpers import add_user, fake_jwt
from vl.app import app
from vl.commands import network
from vl.lib import nodeops, store
from vl.lib.nodeops import NodeResult, Outcome

runner = CliRunner()
WIDE = {"COLUMNS": "200"}
RESET_URL = "http://localhost:8082/v1/network/reset"


@pytest.fixture
def fleet(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """globo (2 nodes), acme (3) and zeta (1), all having run once, with the node
    operations faked: ``stop`` and ``cp`` say how each ``(org, n)`` behaves."""
    store.ensure_local_environment_seeded()
    for i, org in enumerate(("globo", "acme", "zeta", "veritaslock")):
        store.upsert_organization("local", org, f"org-{i}", org.title(), True, f"2026-01-0{i + 1}T00:00:00Z")
    for org, count in (("globo", 2), ("acme", 3), ("zeta", 1)):
        for n in range(1, count + 1):
            sa = store.add_identity("local", "SERVICE_ACCOUNT", f"sa-{org}-{n}", f"{org}-{n}", f"{org}-{n}")
            store.set_svc_acct(sa.id, "local", org, f"client-{org}-{n}", "secret", role="NODE")
            store.set_node_stopped(store.set_pid(store.add_node("local", org, n, sa.id), 1))
    ident = add_user("ops", [{"orgId": "org-3", "role": "PLATFORM_ADMIN"}])
    now = datetime.now(timezone.utc)
    token = fake_jwt("u-ops", typ="user", orgs=[{"orgId": "org-3", "role": "PLATFORM_ADMIN"}])
    store.set_cached_token(ident.id, token, now, now + timedelta(hours=1))

    world: dict[str, Any] = {"stop": {}, "cp": {}, "reset": {}}

    def stop_node(environment: store.Environment, org: str, n: int, emit: Any, **_: Any) -> NodeResult:
        outcome = world["stop"].get((org, n), "stopped")
        if outcome == "already":
            emit(f"{org}/node{n} is not running.")
            return NodeResult(Outcome.ALREADY_STOPPED, "not running")
        if outcome == "wont-die":
            emit(f"Failure to shutdown {org}/node{n} (pid: 4{n}).")
            return NodeResult(Outcome.FAILED, f"still running after SIGTERM and SIGKILL (pid 4{n})", pid=4)
        emit(f"Stopped {org}/node{n} (pid: 4{n}).")
        return NodeResult(Outcome.STOPPED, f"pid 4{n}", pid=4)

    def node_is_up(environment: store.Environment, node: store.Node) -> bool:
        return world["cp"].get((node.org_name, node.org_node)) == "UP"

    def fetch(environment: store.Environment, node: store.Node) -> dict[str, Any]:
        return {"state": world["cp"].get((node.org_name, node.org_node), "DOWN")}

    def reset_node(environment: store.Environment, org: str, n: int, emit: Any, **_: Any) -> NodeResult:
        emit(f"Deleted {org}/node{n}'s data and snapshots directories.")
        return NodeResult(Outcome.RESET, "deleted")

    monkeypatch.setattr(nodeops, "stop_node", stop_node)
    monkeypatch.setattr(nodeops, "node_is_up", node_is_up)
    monkeypatch.setattr(nodeops, "fetch_node_by_id", fetch)
    monkeypatch.setattr(nodeops, "reset_node", reset_node)
    monkeypatch.setattr(nodeops.time, "sleep", lambda s: None)
    return world


# --------------------------------------------------------------------------- #
# vl org stop
# --------------------------------------------------------------------------- #


def test_org_stop_summarizes_every_failure_and_exits_1(fleet: dict[str, Any]) -> None:
    fleet["stop"][("acme", 2)] = "wont-die"
    fleet["cp"][("acme", 2)] = "UP"
    fleet["cp"][("acme", 3)] = "UP"  # stopped, but the control plane never says DOWN

    result = runner.invoke(app, ["org", "stop", "acme"], env=WIDE)

    assert result.exit_code == 1
    out = result.output
    # the per-node lines stay, in order, then the summary at the end
    assert out.index("Stopped acme/node3") < out.index("Warning: acme-node3 is still reported UP")
    assert "Failure to shutdown acme/node2 (pid: 42)." in out
    assert "Stopped acme/node1 (pid: 41)." in out
    assert out.rstrip().endswith(
        "Error: 2 of 3 nodes in acme failed to stop:\n"
        "  acme/node3: still reported UP by the control plane\n"
        "  acme/node2: still reported UP by the control plane"
    )


def test_org_stop_an_already_stopped_node_is_not_a_failure(fleet: dict[str, Any]) -> None:
    fleet["stop"][("acme", 1)] = "already"

    result = runner.invoke(app, ["org", "stop", "acme"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "acme/node1 is not running." in result.output
    assert "failed" not in result.output and "Error" not in result.output


def test_org_stop_a_stop_that_failed_but_reports_down_is_still_a_failure(fleet: dict[str, Any]) -> None:
    fleet["stop"][("zeta", 1)] = "wont-die"  # the control plane says DOWN anyway

    result = runner.invoke(app, ["org", "stop", "zeta"], env=WIDE)

    assert result.exit_code == 1
    assert "1 of 1 nodes in zeta failed to stop:\n  zeta/node1: still running after SIGTERM and SIGKILL (pid 41)" in result.output


# --------------------------------------------------------------------------- #
# vl network stop
# --------------------------------------------------------------------------- #


def test_network_stop_summarizes_the_orgs_that_failed_and_exits_1(fleet: dict[str, Any]) -> None:
    fleet["cp"][("acme", 1)] = "UP"

    result = runner.invoke(app, ["network", "stop"], env=WIDE)

    assert result.exit_code == 1
    out = result.output
    assert "Stopped zeta/node1" in out and "Stopped globo/node1" in out  # the rest still stop
    assert "Error: 1 of 3 nodes in acme failed to stop:" in out  # acme's own error, as it happened
    assert "Warning: 1" not in out
    assert out.rstrip().endswith("Error: 1 of 3 orgs failed:\n  acme: failed, see above")


def test_network_stop_an_exception_keeps_its_warning_and_is_counted(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_stop = network.org.stop

    def stop(name: str, **kw: Any) -> None:
        if name == "zeta":
            raise RuntimeError("the store went away")
        real_stop(name, **kw)

    monkeypatch.setattr(network.org, "stop", stop)
    result = runner.invoke(app, ["network", "stop"], env=WIDE)

    assert result.exit_code == 1
    assert "Warning: the store went away" in result.output
    assert result.output.rstrip().endswith("Error: 1 of 3 orgs failed:\n  zeta: the store went away")


def test_network_stop_already_stopped_nodes_are_not_failures(fleet: dict[str, Any]) -> None:
    for org, n in (("globo", 1), ("acme", 2), ("zeta", 1)):
        fleet["stop"][(org, n)] = "already"

    result = runner.invoke(app, ["network", "stop"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "failed" not in result.output and "Error" not in result.output


# --------------------------------------------------------------------------- #
# vl network reset
# --------------------------------------------------------------------------- #


@respx.mock
def test_network_reset_an_org_that_refuses_is_failed_see_above_not_warning_1(fleet: dict[str, Any]) -> None:
    respx.post(RESET_URL, params={"dryRun": "false"}).mock(
        return_value=httpx.Response(200, json={"dryRun": False, "deleted": {}})
    )
    fleet["cp"][("acme", 3)] = "UP"  # came up after the control plane's reset

    result = runner.invoke(app, ["network", "reset", "--yes"], env=WIDE)

    assert result.exit_code == 1
    out = result.output
    assert "Error: Can't reset org 'acme': every node must be down, and these are not:" in out
    assert "Warning: 1" not in out
    assert "Deleted globo/node1's" in out and "Deleted zeta/node1's" in out  # the rest still reset
    assert out.rstrip().endswith("Error: 1 of 3 orgs failed:\n  acme: failed, see above")


@respx.mock
def test_network_reset_without_failures_exits_0(fleet: dict[str, Any]) -> None:
    respx.post(RESET_URL, params={"dryRun": "false"}).mock(
        return_value=httpx.Response(200, json={"dryRun": False, "deleted": {}})
    )

    result = runner.invoke(app, ["network", "reset", "--yes"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "failed" not in result.output and "Error" not in result.output
