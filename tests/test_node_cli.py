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


# --------------------------------------------------------------------------- #
# list (§5.5): local rows in the identity's scope, with the CP overlay
# --------------------------------------------------------------------------- #

import json as _json
from datetime import datetime, timedelta, timezone

import httpx
import respx

CP_NODES = "http://localhost:8082/v1/nodes"
IDP_LOGIN = "http://localhost:8080/auth/user/login"
HERE = "192.168.8.2"
WIDE = {"COLUMNS": "200"}


def _user(label: str = "ops", memberships: dict[str, str] | None = None, *, default: bool = True) -> store.Identity:
    """A tokened USER identity with cached org memberships (org name -> role)."""
    store.ensure_local_environment_seeded()
    ident = store.add_identity("local", "USER", f"u-{label}", label, label)
    store.set_user_acct(ident.id, "pw")
    for org, role in (memberships or {}).items():
        store.upsert_org_membership(ident.id, "local", org, role)
    if default:
        store.set_default_identity("local", label)
    now = datetime.now(timezone.utc)
    store.set_cached_token(ident.id, f"{label}-jwt", now, now + timedelta(hours=1))
    return ident


def _cp_row(org_id: str, n: int, state: str, *, host: str | None = HERE, pid: str | None = None) -> dict[str, Any]:
    return {
        "id": f"{org_id}-{n}", "orgId": org_id, "orgNode": n, "state": state, "host": host,
        "port": 7000 + n, "metadataJson": None if pid is None else _json.dumps({"pid": pid}),
    }


def _cp(*rows: dict[str, Any], next_cursor: str | None = None) -> httpx.Response:
    return httpx.Response(200, json={"items": list(rows), "nextCursor": next_cursor})


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch, kills: Kills) -> dict[str, Any]:
    """Injected process and host facts: which pids are live verilocks, process ages."""
    facts: dict[str, Any] = {"cp_alive": set(), "age": {}}
    monkeypatch.setattr(node_cmd.host, "local_identities", lambda: {HERE, "127.0.0.1"})
    monkeypatch.setattr(node_cmd.proc, "is_cp_process", lambda pid, **_: pid in facts["cp_alive"])
    monkeypatch.setattr(
        node_cmd.proc, "process_age_seconds", lambda pid, **_: facts["age"].get(pid, 3600.0)
    )
    return facts


def _states(result: Any) -> dict[tuple[str, int], str]:
    rows = _json.loads(result.stdout)
    return {(r["org"], r["node"]): r["state"] for r in rows}


def _list(*args: str, env: dict[str, str] | None = None) -> Any:
    return runner.invoke(app, ["node", "list", *args], env={"VL_OUTPUT": "json", **(env or {})})


def test_list_without_an_identity_fails_and_prints_nothing(machine: dict) -> None:
    _org()
    _node()
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(CP_NODES)
        result = runner.invoke(app, ["node", "list"], env=WIDE)
    assert result.exit_code == 1
    assert "No identity selected" in result.output
    assert "nodes in" not in result.output
    assert not route.called


@respx.mock
def test_list_overlays_the_cp_and_prints_the_spec_columns(kills: Kills, machine: dict) -> None:
    _org()
    _user(memberships={"globo": "ORG_ADMIN"})
    _node(n=1, pid=4312)
    kills.alive.add(4312)
    _node(n=2, port=7002)  # NEW: the CP has never heard of it
    route = respx.get(CP_NODES).mock(return_value=_cp(_cp_row("org-1", 1, "UP", pid="4312")))

    result = _list()

    assert result.exit_code == 0, result.output
    rows = _json.loads(result.stdout)
    assert [list(r) for r in rows] == [["org", "node", "port", "state", "pid", "created_at"]] * 2
    assert _states(result) == {("globo", 1): "RUNNING", ("globo", 2): "NEW"}
    request = route.calls.last.request
    assert "scope" not in request.url.params  # list form, scoped by caller
    assert request.headers["authorization"] == "Bearer ops-jwt"


@respx.mock
def test_list_shows_labels_when_the_views_diverge(kills: Kills, machine: dict) -> None:
    _org()
    _user(memberships={"globo": "ORG_ADMIN"})
    _node(n=1, pid=4312)            # alive here, CP says DOWN with our pid
    _node(n=2, port=7002, pid=4400)  # alive here, CP DOWN with another pid
    kills.alive.update({4312, 4400})
    store.set_node_stopped(_node(n=3, port=7003))  # stopped here, UP elsewhere
    store.set_node_stopped(_node(n=4, port=7004))  # stopped here, UP here from a debugger
    machine["cp_alive"].add(9001)
    respx.get(CP_NODES).mock(return_value=_cp(
        _cp_row("org-1", 1, "DOWN", pid="4312"),
        _cp_row("org-1", 2, "DOWN", pid="1234"),
        _cp_row("org-1", 3, "UP", host="10.0.0.9", pid="77"),
        _cp_row("org-1", 4, "STANDBY", pid="9001"),
    ))

    result = _list()

    assert result.exit_code == 0, result.output
    assert _states(result) == {
        ("globo", 1): "NO_HEARTBEAT",
        ("globo", 2): "UNREGISTERED",
        ("globo", 3): "REMOTE",
        ("globo", 4): "UNMANAGED",
    }
    # The labels are display only: the stored states are untouched.
    assert [store.get_node("local", "globo", n).node_state for n in (1, 2, 3, 4)] == [
        "RUNNING", "RUNNING", "STOPPED", "STOPPED",
    ]


@respx.mock
def test_list_grace_period_hides_unregistered_for_a_young_process(kills: Kills, machine: dict) -> None:
    _org()
    _user(memberships={"globo": "ORG_ADMIN"})
    _node(pid=4312)
    kills.alive.add(4312)
    machine["age"][4312] = 2.0
    respx.get(CP_NODES).mock(return_value=_cp(_cp_row("org-1", 1, "DOWN", pid=None)))

    assert _states(_list()) == {("globo", 1): "RUNNING"}


@respx.mock
def test_list_matches_cp_rows_by_server_org_id(kills: Kills, machine: dict) -> None:
    _org("globo", "org-1")
    _org("acme", "org-2")
    _user(memberships={"globo": "ORG_ADMIN", "acme": "USER"})
    store.set_node_stopped(_node("globo", 1))
    store.set_node_stopped(_node("acme", 1, port=7002))
    respx.get(CP_NODES).mock(return_value=_cp(
        _cp_row("org-2", 1, "UP", host="10.0.0.9"),   # acme/node1, elsewhere
        _cp_row("org-9", 1, "UP", host="10.0.0.9"),   # an org this store never cached
    ))

    assert _states(_list()) == {("acme", 1): "REMOTE", ("globo", 1): "STOPPED"}


@respx.mock
def test_list_pages_the_cp_to_exhaustion(kills: Kills, machine: dict) -> None:
    _org()
    _user(memberships={"globo": "ORG_ADMIN"})
    store.set_node_stopped(_node(n=1))
    store.set_node_stopped(_node(n=2, port=7002))
    first = respx.get(CP_NODES, params={"cursor": "c2"}).mock(
        return_value=_cp(_cp_row("org-1", 2, "UP", host="10.0.0.9"))
    )
    respx.get(CP_NODES).mock(
        return_value=_cp(_cp_row("org-1", 1, "UP", host="10.0.0.9"), next_cursor="c2")
    )

    result = _list()

    assert result.exit_code == 0, result.output
    assert first.called
    assert _states(result) == {("globo", 1): "REMOTE", ("globo", 2): "REMOTE"}


# --- scoping (§5.5 step 2) ------------------------------------------------- #


def _two_orgs() -> None:
    _org("globo", "org-1")
    _org("acme", "org-2")
    _node("globo", 1)
    _node("acme", 1, port=7002)


@respx.mock
def test_list_scopes_to_the_users_orgs(machine: dict) -> None:
    _two_orgs()
    _user(memberships={"acme": "USER"})
    respx.get(CP_NODES).mock(return_value=_cp())
    assert set(_states(_list())) == {("acme", 1)}


@respx.mock
def test_list_shows_every_org_for_a_platform_admin(machine: dict) -> None:
    _two_orgs()
    _user(memberships={"acme": "PLATFORM_ADMIN"})
    respx.get(CP_NODES).mock(return_value=_cp())
    assert set(_states(_list())) == {("acme", 1), ("globo", 1)}


@respx.mock
def test_list_scopes_a_service_account_to_its_own_org(machine: dict) -> None:
    _two_orgs()
    sa = store.add_identity("local", "SERVICE_ACCOUNT", "sa-ops", "ops-sa", "ops-sa")
    store.set_svc_acct(sa.id, "local", "globo", "client-ops", "secret", role="SYSTEM")
    now = datetime.now(timezone.utc)
    store.set_cached_token(sa.id, "sa-jwt", now, now + timedelta(hours=1))
    respx.get(CP_NODES).mock(return_value=_cp())

    assert set(_states(_list("--as", "ops-sa"))) == {("globo", 1)}


@respx.mock
def test_list_org_option_narrows_within_scope(machine: dict) -> None:
    _two_orgs()
    _user(memberships={"acme": "PLATFORM_ADMIN"})
    respx.get(CP_NODES).mock(return_value=_cp())
    assert set(_states(_list("--org", "globo"))) == {("globo", 1)}


@respx.mock
def test_list_org_option_outside_scope_lists_nothing(machine: dict) -> None:
    _two_orgs()
    _user(memberships={"acme": "USER"})
    respx.get(CP_NODES).mock(return_value=_cp())
    result = _list("--org", "globo")
    assert result.exit_code == 0, result.output
    assert _json.loads(result.stdout) == []


@respx.mock
def test_list_as_selects_a_non_default_identity(machine: dict) -> None:
    _two_orgs()
    _user("ops", memberships={"acme": "USER"})
    _user("admin", memberships={"globo": "ORG_ADMIN"}, default=False)
    route = respx.get(CP_NODES).mock(return_value=_cp())

    assert set(_states(_list("--as", "admin"))) == {("globo", 1)}
    assert route.calls.last.request.headers["authorization"] == "Bearer admin-jwt"


# --- repair and errors ------------------------------------------------------ #


@respx.mock
def test_list_repairs_a_dead_running_row_and_persists_it(kills: Kills, machine: dict) -> None:
    _org()
    _user(memberships={"globo": "ORG_ADMIN"})
    _node(pid=4312)  # not alive
    respx.get(CP_NODES).mock(return_value=_cp(_cp_row("org-1", 1, "UP", pid="4312")))

    result = _list()

    # The CP still says UP (its 30-35s lag), but the pid isn't alive here: STOPPED.
    assert _states(result) == {("globo", 1): "STOPPED"}
    row = store.get_node("local", "globo", 1)
    assert row.node_state == "STOPPED" and row.pid is None
    assert _json.loads(result.stdout)[0]["pid"] is None


@respx.mock
def test_list_403_prints_the_cp_error_and_nothing_else(machine: dict) -> None:
    _org()
    _user(memberships={"globo": "ORG_ADMIN"})
    _node()
    respx.get(CP_NODES).mock(
        return_value=httpx.Response(403, json={"detail": "Not authorized to list nodes."})
    )

    result = runner.invoke(app, ["node", "list"], env=WIDE)

    assert result.exit_code == 1
    assert "Not authorized to list nodes." in result.output
    assert "nodes in" not in result.output and "globo" not in result.output


@respx.mock
def test_list_401_after_reauth_prints_the_cp_error(machine: dict) -> None:
    _org()
    _user(memberships={"globo": "ORG_ADMIN"})
    _node()
    respx.post(IDP_LOGIN).mock(
        return_value=httpx.Response(200, json={"accessToken": "fresh-jwt", "expiresIn": 900})
    )
    cp = respx.get(CP_NODES).mock(
        return_value=httpx.Response(401, json={"detail": "Token rejected."})
    )

    result = runner.invoke(app, ["node", "list"], env=WIDE)

    assert result.exit_code == 1
    assert "Token rejected." in result.output
    assert cp.call_count == 2  # retried once with a fresh token, as authed_call does
    assert "nodes in" not in result.output


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.Response(503, json={"detail": "Service unavailable"}),
        httpx.Response(500, text="boom"),
    ],
    ids=["connect", "timeout", "503", "500"],
)
def test_list_unreachable_cp_warns_and_shows_local_state(kills: Kills, machine: dict, failure: Any) -> None:
    _org()
    _user(memberships={"globo": "ORG_ADMIN"})
    _node(n=1, pid=4312)
    kills.alive.add(4312)
    _node(n=2, port=7002, pid=4400)  # dead: repaired even with no CP

    with respx.mock:
        route = respx.get(CP_NODES)
        if isinstance(failure, httpx.Response):
            route.mock(return_value=failure)
        else:
            route.mock(side_effect=failure)
        result = _list()

    assert result.exit_code == 0, result.output
    assert "warning: control plane unreachable; showing local state only" in result.stderr
    assert "warning" not in result.stdout  # JSON on stdout stays parseable
    assert _states(result) == {("globo", 1): "RUNNING", ("globo", 2): "STOPPED"}


# --------------------------------------------------------------------------- #
# show (§5.6): the same identity, scope and CP handling as list
# --------------------------------------------------------------------------- #


def _show(*args: str, json_out: bool = False) -> Any:
    env = {"VL_OUTPUT": "json"} if json_out else WIDE
    return runner.invoke(app, ["node", "show", *args], env=env)


@respx.mock
def test_show_prints_the_spec_fields_and_no_reason_when_states_agree(kills: Kills, machine: dict) -> None:
    _org()
    _user(memberships={"globo": "ORG_ADMIN"})
    _node(pid=4312)
    kills.alive.add(4312)
    respx.get(CP_NODES).mock(return_value=_cp(_cp_row("org-1", 1, "UP", pid="4312")))

    result = _show("globo", "1", json_out=True)

    assert result.exit_code == 0, result.output
    row = _json.loads(result.stdout)
    assert list(row) == [
        "org_node", "port", "pid", "state", "created_at",
        "client_id", "private_key_path", "public_key_path",
    ]
    assert row["state"] == "RUNNING" and row["pid"] == 4312
    assert row["client_id"] == "client-globo-1"
    assert row["private_key_path"] == "/keys/globo-1/private.key"


@respx.mock
def test_show_explains_a_label(kills: Kills, machine: dict) -> None:
    _org()
    _user(memberships={"globo": "ORG_ADMIN"})
    _node(pid=4312)
    kills.alive.add(4312)
    respx.get(CP_NODES).mock(return_value=_cp(_cp_row("org-1", 1, "DOWN", pid="4312")))

    table = _show("globo", "1")
    as_json = _show("globo", "1", json_out=True)

    reason = "stored RUNNING; control plane DOWN on 192.168.8.2, pid 4312 (this node: pid 4312)"
    assert table.exit_code == 0, table.output
    assert "NO_HEARTBEAT" in table.stdout and reason in table.stdout
    row = _json.loads(as_json.stdout)
    assert row["state"] == "NO_HEARTBEAT" and row["reason"] == reason


@respx.mock
def test_show_explains_remote_with_unknown_values(machine: dict) -> None:
    _org()
    _user(memberships={"globo": "ORG_ADMIN"})
    store.set_node_stopped(_node())
    respx.get(CP_NODES).mock(return_value=_cp(_cp_row("org-1", 1, "UP", host="10.0.0.9", pid=None)))

    row = _json.loads(_show("globo", "1", json_out=True).stdout)

    assert row["state"] == "REMOTE"
    assert row["reason"] == "stored STOPPED; control plane UP on 10.0.0.9, pid unknown (this node: no pid)"


@respx.mock
def test_show_agrees_with_list(kills: Kills, machine: dict) -> None:
    _org()
    _user(memberships={"globo": "ORG_ADMIN"})
    _node(pid=4312)
    kills.alive.add(4312)
    respx.get(CP_NODES).mock(return_value=_cp(_cp_row("org-1", 1, "UP", pid="9999")))

    listed = _states(_list())[("globo", 1)]
    shown = _json.loads(_show("globo", "1", json_out=True).stdout)["state"]
    assert listed == shown == "UNKNOWN"


def test_show_outside_the_identitys_scope_is_not_found(machine: dict) -> None:
    _two_orgs()
    _user(memberships={"acme": "USER"})
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(CP_NODES).mock(return_value=_cp())
        result = _show("globo", "1")
    assert result.exit_code == 1
    assert "No node globo/node1" in result.output and "visible to 'ops'" in result.output
    assert not route.called


def test_show_missing_node_is_not_found(machine: dict) -> None:
    _org()
    _user(memberships={"globo": "ORG_ADMIN"})
    with respx.mock(assert_all_called=False):
        result = _show("globo", "7")
    assert result.exit_code == 1
    assert "No node globo/node7" in result.output


def test_show_without_an_identity_fails(machine: dict) -> None:
    _org()
    _node()
    result = _show("globo", "1")
    assert result.exit_code == 1
    assert "No identity selected" in result.output


@respx.mock
def test_show_403_prints_the_cp_error_and_nothing_else(machine: dict) -> None:
    _org()
    _user(memberships={"globo": "ORG_ADMIN"})
    _node()
    respx.get(CP_NODES).mock(return_value=httpx.Response(403, json={"detail": "Not authorized."}))

    result = _show("globo", "1")

    assert result.exit_code == 1
    assert "Not authorized." in result.output
    assert "client_id" not in result.output and "Org: globo" not in result.output


def test_show_unreachable_cp_warns_and_shows_repaired_local_state(kills: Kills, machine: dict) -> None:
    _org()
    _user(memberships={"globo": "ORG_ADMIN"})
    _node(pid=4312)  # dead

    with respx.mock:
        respx.get(CP_NODES).mock(side_effect=httpx.ConnectError("refused"))
        result = _show("globo", "1", json_out=True)

    assert result.exit_code == 0, result.output
    assert "warning: control plane unreachable; showing local state only" in result.stderr
    row = _json.loads(result.stdout)
    assert row["state"] == "STOPPED" and row["pid"] is None and "reason" not in row
