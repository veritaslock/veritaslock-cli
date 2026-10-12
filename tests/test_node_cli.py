"""Tests for the `vl node` command group.

Process information is injected by replacing ``vl.lib.proc``'s checks, and
``os.kill`` is replaced with a recorder, so no real process is ever signalled.
"""

from __future__ import annotations

import signal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from vl.app import app
from vl.commands import node as node_cmd
from vl.lib import store

runner = CliRunner()


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Node roots live under $HOME/orgs; keep them in the test's tmp dir."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


def _org(name: str = "globo", server_org_id: str = "org-1") -> None:
    store.ensure_local_environment_seeded()
    store.upsert_organization(
        "local", name, server_org_id, name.title(), True, "2026-01-01T00:00:00Z"
    )


def _node(org: str = "globo", n: int = 1, *, pid: int | None = None, port: int = 7001) -> store.Node:
    """A node row (with its NODE service account); RUNNING with ``pid`` if given."""
    ident = store.add_identity("local", "SERVICE_ACCOUNT", f"sa-{org}-{n}", f"{org}-{n}", f"{org}-{n}")
    store.set_svc_acct(
        ident.id, "local", org, f"client-{org}-{n}", "secret",
        public_key_path=f"/keys/{org}-{n}/public.key",
        private_key_path=f"/keys/{org}-{n}/private.key",
        role="NODE",
    )
    row = store.add_node("local", org, n, ident.id, port)
    if pid is not None:
        row = store.set_pid(row, pid)
    return row


class Kills:
    """Records os.kill calls; the processes in ``alive`` stop being ours on SIGTERM."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []
        self.alive: set[int] = set()

    def kill(self, pid: int, sig: int) -> None:
        self.calls.append((pid, sig))
        if sig in (signal.SIGTERM, signal.SIGKILL):
            self.alive.discard(pid)


@pytest.fixture
def kills(monkeypatch: pytest.MonkeyPatch) -> Kills:
    k = Kills()
    monkeypatch.setattr(node_cmd.os, "kill", k.kill)
    monkeypatch.setattr(
        node_cmd.proc, "is_our_process", lambda pid, exe, **_: pid in k.alive
    )
    monkeypatch.setattr(node_cmd.time, "sleep", lambda s: None)
    return k


# --------------------------------------------------------------------------- #
# stop (§5.3): only ever signals this node's own process
# --------------------------------------------------------------------------- #


def test_stop_never_signals_a_stale_pid(kills: Kills) -> None:
    _org()
    _node(pid=4312)  # 4312 is not in kills.alive: dead, or reused by another process

    result = runner.invoke(app, ["node", "stop", "globo", "1"])

    assert result.exit_code == 0, result.output
    assert kills.calls == []
    row = store.get_node("local", "globo", 1)
    assert row.node_state == "STOPPED" and row.pid is None


def test_stop_terminates_its_own_process(kills: Kills) -> None:
    _org()
    _node(pid=4312)
    kills.alive.add(4312)

    result = runner.invoke(app, ["node", "stop", "globo", "1"])

    assert result.exit_code == 0, result.output
    assert kills.calls == [(4312, signal.SIGTERM)]
    assert "Stopped globo/node1 (pid: 4312)" in result.output
    row = store.get_node("local", "globo", 1)
    assert row.node_state == "STOPPED" and row.pid is None


def test_stop_checks_the_nodes_own_binary(kills: Kills, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(
        node_cmd.proc, "is_our_process", lambda pid, exe, **_: seen.append((pid, exe)) or False
    )
    _org()
    _node(pid=4312)

    runner.invoke(app, ["node", "stop", "globo", "1"])

    assert seen == [(4312, home / "orgs" / "globo" / "nodes" / "node1" / "bin" / "verilock")]


def test_stop_without_a_pid_does_nothing(kills: Kills) -> None:
    _org()
    _node()

    result = runner.invoke(app, ["node", "stop", "globo", "1"])

    assert result.exit_code == 0, result.output
    assert "not running" in result.output
    assert kills.calls == []
    assert store.get_node("local", "globo", 1).node_state == "NEW"


# --------------------------------------------------------------------------- #
# start's pre-check (§5.2 step 1) uses the same check
# --------------------------------------------------------------------------- #


def test_start_precheck_treats_a_stale_pid_as_down(kills: Kills) -> None:
    _org()
    row = _node(pid=4312)
    assert node_cmd.is_node_down(row)


def test_start_precheck_refuses_when_its_own_process_runs(kills: Kills) -> None:
    _org()
    row = _node(pid=4312)
    kills.alive.add(4312)
    assert not node_cmd.is_node_down(row)


# --------------------------------------------------------------------------- #
# repair on read (§4c): RUNNING rows whose process is gone become STOPPED
# --------------------------------------------------------------------------- #


def test_repair_stops_a_running_row_whose_process_is_gone(kills: Kills) -> None:
    _org()
    row = _node(pid=4312)

    repaired = node_cmd.repair(row)

    assert repaired.node_state == "STOPPED" and repaired.pid is None
    stored = store.get_node("local", "globo", 1)
    assert stored.node_state == "STOPPED" and stored.pid is None  # persisted
    assert kills.calls == []  # nothing signalled


def test_repair_leaves_a_live_running_row_alone(kills: Kills) -> None:
    _org()
    row = _node(pid=4312)
    kills.alive.add(4312)

    assert node_cmd.repair(row) == row
    assert store.get_node("local", "globo", 1).node_state == "RUNNING"


def test_repair_leaves_new_and_stopped_rows_alone(kills: Kills) -> None:
    _org()
    new = _node(n=1)
    stopped = store.set_node_stopped(_node(n=2, port=7002))

    assert node_cmd.repair(new) == new
    assert node_cmd.repair(stopped) == stopped
