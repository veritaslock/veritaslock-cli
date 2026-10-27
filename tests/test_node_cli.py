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

from _helpers import add_service_account, add_user
from vl.app import app
from vl.commands import node as node_cmd
from vl.lib import store

runner = CliRunner()


@pytest.fixture
def operator() -> store.Identity:
    """The default identity: ORG_ADMIN of globo (org-1), so node commands on globo pass."""
    return add_user("opadmin", [{"orgId": "org-1", "role": "ORG_ADMIN"}])


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


def test_stop_never_signals_a_stale_pid(kills: Kills, operator: store.Identity) -> None:
    _org()
    _node(pid=4312)  # 4312 is not in kills.alive: dead, or reused by another process

    result = runner.invoke(app, ["node", "stop", "globo", "1"])

    assert result.exit_code == 0, result.output
    assert kills.calls == []
    row = store.get_node("local", "globo", 1)
    assert row.node_state == "STOPPED" and row.pid is None


def test_stop_terminates_its_own_process(kills: Kills, operator: store.Identity) -> None:
    _org()
    _node(pid=4312)
    kills.alive.add(4312)

    result = runner.invoke(app, ["node", "stop", "globo", "1"])

    assert result.exit_code == 0, result.output
    assert kills.calls == [(4312, signal.SIGTERM)]
    assert "Stopped globo/node1 (pid: 4312)" in result.output
    row = store.get_node("local", "globo", 1)
    assert row.node_state == "STOPPED" and row.pid is None


def test_stop_checks_the_nodes_own_binary(
    kills: Kills, home: Path, monkeypatch: pytest.MonkeyPatch, operator: store.Identity
) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(
        node_cmd.proc, "is_our_process", lambda pid, exe, **_: seen.append((pid, exe)) or False
    )
    _org()
    _node(pid=4312)

    runner.invoke(app, ["node", "stop", "globo", "1"])

    assert seen == [(4312, home / "orgs" / "globo" / "nodes" / "node1" / "bin" / "verilock")]


def test_stop_without_a_pid_does_nothing(kills: Kills, operator: store.Identity) -> None:
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


# --------------------------------------------------------------------------- #
# authorization (vl-authorization-spec.md): start, stop and reset need
# ORG_ADMIN of the node's org or PLATFORM_ADMIN, checked before anything else
# --------------------------------------------------------------------------- #

from types import SimpleNamespace


class Work:
    """Everything start, stop and reset can do to the machine, recorded."""

    def __init__(self, home: Path) -> None:
        self.spawned: list[tuple[str, int]] = []
        self.data = home / "orgs" / "globo" / "nodes" / "node1" / "data"
        self.data.mkdir(parents=True)
        (self.data / "chain.db").write_text("blocks")

    def data_intact(self) -> bool:
        return (self.data / "chain.db").exists()


@pytest.fixture
def work(home: Path, kills: Kills, monkeypatch: pytest.MonkeyPatch) -> Work:
    w = Work(home)
    monkeypatch.setattr(node_cmd, "populate_node_dir", lambda env, org, n, node: home)
    monkeypatch.setattr(
        node_cmd, "start_verilock", lambda node, root, org, n: w.spawned.append((org, n))
    )
    return w


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        node_cmd, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True))
    )


def _globo_and_acme() -> None:
    _org("globo", "org-1")
    _org("acme", "org-2")


COMMANDS = {
    "start": ["node", "start", "globo", "1"],
    "stop": ["node", "stop", "globo", "1"],
    "reset": ["node", "reset", "globo", "1", "--yes"],
}


def _did_work(command: str, w: Work, kills: Kills) -> bool:
    return {
        "start": bool(w.spawned),
        "stop": bool(kills.calls),
        "reset": not w.data_intact(),
    }[command]


def _setup_node(command: str, kills: Kills) -> None:
    _globo_and_acme()
    if command == "stop":
        _node(pid=4312)
        kills.alive.add(4312)
    else:
        _node()  # NEW: start spawns it, reset needs no CP check


@pytest.mark.parametrize("command", list(COMMANDS))
@pytest.mark.parametrize(
    "orgs",
    [
        [{"orgId": "org-1", "role": "ORG_ADMIN"}],
        [{"orgId": "veritaslock", "role": "PLATFORM_ADMIN"}],
        [{"orgId": "org-2", "role": "PLATFORM_ADMIN"}],
    ],
    ids=["org-admin", "platform-admin", "platform-admin-other-org"],
)
def test_node_command_is_allowed_for_an_admin(command: str, orgs: list, work: Work, kills: Kills) -> None:
    _setup_node(command, kills)
    add_user("ops", orgs)

    result = runner.invoke(app, COMMANDS[command], env=WIDE)

    assert result.exit_code == 0, result.output
    assert _did_work(command, work, kills)


REFUSALS = {
    "user": ([{"orgId": "org-1", "role": "USER"}], "is not an admin of 'globo'"),
    "key-reader": ([{"orgId": "org-1", "role": "KEY_READER"}], "is not an admin of 'globo'"),
    "admin-of-another-org": ([{"orgId": "org-2", "role": "ORG_ADMIN"}], "is not an admin of 'globo'"),
}


@pytest.mark.parametrize("command", list(COMMANDS))
@pytest.mark.parametrize("who", list(REFUSALS))
def test_node_command_is_refused_for_a_non_admin_and_does_nothing(
    command: str, who: str, work: Work, kills: Kills
) -> None:
    _setup_node(command, kills)
    orgs, message = REFUSALS[who]
    add_user("ops", orgs)

    result = runner.invoke(app, COMMANDS[command], env=WIDE)

    assert result.exit_code == 1
    assert message in result.output
    assert not _did_work(command, work, kills)


@pytest.mark.parametrize("command", list(COMMANDS))
@pytest.mark.parametrize("role", ["NODE", "SYSTEM"])
def test_node_command_is_refused_for_a_service_account(
    command: str, role: str, work: Work, kills: Kills
) -> None:
    _setup_node(command, kills)
    # NODE: the node's own service account, which _node() created as 'globo-1'.
    label = "globo-1" if role == "NODE" else "ops-system"
    if role == "SYSTEM":
        add_service_account(label, "globo", role)

    result = runner.invoke(app, [*COMMANDS[command], "--as", label], env=WIDE)

    assert result.exit_code == 1
    assert "service accounts can't run this command" in result.output
    assert not _did_work(command, work, kills)


@pytest.mark.parametrize("command", list(COMMANDS))
def test_node_command_without_an_identity_is_refused(command: str, work: Work, kills: Kills) -> None:
    _setup_node(command, kills)

    result = runner.invoke(app, COMMANDS[command], env=WIDE)

    assert result.exit_code == 1
    assert "No identity selected" in result.output
    assert not _did_work(command, work, kills)


def test_as_overrides_the_default_identity(work: Work, kills: Kills) -> None:
    _setup_node("start", kills)
    add_user("ops", [{"orgId": "org-1", "role": "USER"}])
    add_user("boss", [{"orgId": "org-1", "role": "ORG_ADMIN"}], default=False)

    assert runner.invoke(app, COMMANDS["start"], env=WIDE).exit_code == 1
    result = runner.invoke(app, [*COMMANDS["start"], "--as", "boss"], env=WIDE)
    assert result.exit_code == 0, result.output
    assert work.spawned == [("globo", 1)]


def test_reset_refusal_never_shows_the_prompt(tty: None, work: Work, kills: Kills) -> None:
    _setup_node("reset", kills)
    add_user("ops", [{"orgId": "org-1", "role": "USER"}])

    result = runner.invoke(app, ["node", "reset", "globo", "1"], input="y\n", env=WIDE)

    assert result.exit_code == 1
    assert "Are you sure" not in result.output
    assert work.data_intact()


def test_reset_prompt_still_works_after_the_check(tty: None, work: Work, kills: Kills) -> None:
    _setup_node("reset", kills)
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])

    declined = runner.invoke(app, ["node", "reset", "globo", "1"], input="n\n", env=WIDE)
    assert declined.exit_code == 0, declined.output
    assert "Are you sure" in declined.output and work.data_intact()

    confirmed = runner.invoke(app, ["node", "reset", "globo", "1"], input="y\n", env=WIDE)
    assert confirmed.exit_code == 0, confirmed.output
    assert "Deleted globo/node1's data directory." in confirmed.output
    assert not work.data_intact() and work.data.is_dir()


def test_start_happy_path_spawns_the_node(work: Work, kills: Kills) -> None:
    _setup_node("start", kills)
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])

    result = runner.invoke(app, COMMANDS["start"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert work.spawned == [("globo", 1)]


def test_reset_happy_path_with_yes_clears_data(work: Work, kills: Kills) -> None:
    _setup_node("reset", kills)
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])

    result = runner.invoke(app, [*COMMANDS["reset"], "--include-snapshots"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "Deleted globo/node1's data and snapshots directories." in result.output
    assert not work.data_intact()


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [("deleted", True), ("declined", False), ("up", False)],
)
def test_reset_returns_whether_it_deleted(
    outcome: str, expected: bool, tty: None, work: Work, kills: Kills, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch caller (`vl org reset`) tells a skipped node from a reset one by this."""
    _globo_and_acme()
    store.set_node_stopped(store.set_pid(_node(), 4312))  # not NEW, so reset checks the CP
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])
    monkeypatch.setattr(node_cmd, "node_is_up", lambda environment, node: outcome == "up")
    monkeypatch.setattr(node_cmd.typer, "confirm", lambda *a, **kw: outcome != "declined")

    assert node_cmd.reset("globo", 1, env="local", as_="ops") is expected
    assert work.data_intact() is not expected


@pytest.mark.parametrize("command", list(COMMANDS))
def test_missing_node_has_a_real_message(command: str, work: Work, kills: Kills) -> None:
    _globo_and_acme()
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])

    args = [a if a != "1" else "7" for a in COMMANDS[command]]
    result = runner.invoke(app, args, env=WIDE)

    assert result.exit_code == 1
    assert "No node globo/node7 in environment 'local'" in result.output


# --------------------------------------------------------------------------- #
# start's liveness check (§5.2 step 4): an early exit is a failure
# --------------------------------------------------------------------------- #

NOT_DOWN_LOG = (
    "2026-10-20 10:00:00 [main] INFO registering globo/node1\n"
    "2026-10-20 10:00:01 [main] ERROR registration failed: "
    "409 (NOT_DOWN): node org_node=1 is not DOWN on the control plane\n"
)


class FakeProcess:
    """Stands in for subprocess.Popen: writes ``log`` to the node's log file, then
    reports ``returncode`` from poll() (``None``: still running)."""

    pid = 367635

    def __init__(self, returncode: int | None, log: str) -> None:
        self.returncode = returncode
        self.log = log

    def __call__(self, args: list[str], *, stdout: Any, **_: Any) -> FakeProcess:
        stdout.write(self.log)
        return self

    def poll(self) -> int | None:
        return self.returncode


@pytest.fixture
def spawn(home: Path, kills: Kills, monkeypatch: pytest.MonkeyPatch, operator: store.Identity) -> Any:
    """Run `vl node start globo 1` against a fake verilock that ends as told."""
    _org()
    _node()
    monkeypatch.setattr(node_cmd, "populate_node_dir", lambda env, org, n, node: home)

    def run(returncode: int | None, log: str = "") -> Any:
        fake = FakeProcess(returncode, log)
        monkeypatch.setattr(node_cmd.subprocess, "Popen", fake)
        return runner.invoke(app, ["node", "start", "globo", "1"], env=WIDE)

    return run


def _assert_stopped() -> None:
    row = store.get_node("local", "globo", 1)
    assert row.pid is None and row.node_state == "STOPPED"


def test_start_killed_by_sigabrt_on_not_down_is_a_failure_with_a_hint(spawn: Any) -> None:
    result = spawn(-6, NOT_DOWN_LOG)

    assert result.exit_code == 1
    assert "Started" not in result.output
    _assert_stopped()
    out = result.output
    first = out.index("Error: globo/node1 exited right after starting (killed by SIGABRT).")
    hint = out.index("The control plane still has this node as UP.")
    tail = out.index("---- Log tail ----")
    line = out.index("[main] ERROR registration failed: 409 (NOT_DOWN)")  # brackets kept
    assert first < hint < tail < line
    assert "Wait about 30 seconds and start it again." in " ".join(out.split())  # Rich wraps


@pytest.mark.parametrize(
    ("code", "hint"),
    [
        (69, "The control plane still has this node as UP. Wait about 30 seconds for its heartbeat timeout"),
        (75, "The node asked to be started again later"),
        (74, "The node stopped because of a storage failure. Do not restart it automatically"),
    ],
)
def test_start_exit_codes_with_a_known_meaning_get_a_hint(spawn: Any, code: int, hint: str) -> None:
    result = spawn(code, "some log line\n")

    assert result.exit_code == 1
    assert f"globo/node1 exited right after starting (exited with code {code})." in result.output
    out = " ".join(result.output.split())  # Rich wraps long lines
    assert hint in out
    assert out.index(hint) < out.index("---- Log tail ----")
    assert "some log line" in result.output
    assert "Started" not in result.output
    _assert_stopped()


def test_start_exit_69_uses_its_own_hint_even_with_not_down_in_the_log(spawn: Any) -> None:
    result = spawn(69, NOT_DOWN_LOG)

    assert result.exit_code == 1
    out = " ".join(result.output.split())  # Rich wraps long lines
    assert "globo/node1 exited right after starting (exited with code 69)." in out
    assert "Wait about 30 seconds for its heartbeat timeout and start it again." in out
    assert "A kill -9" not in out
    assert "409 (NOT_DOWN)" in out  # still in the log tail
    _assert_stopped()


def test_start_other_exit_code_gets_no_hint(spawn: Any) -> None:
    result = spawn(1, "error: missing liblog4cplus.so.3\n")

    assert result.exit_code == 1
    lines = result.output.splitlines()
    assert lines[0] == "Error: globo/node1 exited right after starting (exited with code 1)."
    assert lines[1] == "---- Log tail ----"
    assert lines[2] == "error: missing liblog4cplus.so.3"
    _assert_stopped()


def test_start_unknown_signal_falls_back_to_its_number(spawn: Any) -> None:
    result = spawn(-99)

    assert result.exit_code == 1
    assert "exited right after starting (killed by signal 99)." in result.output
    _assert_stopped()


def test_start_a_live_process_is_started(spawn: Any) -> None:
    result = spawn(None, "listening\n")

    assert result.exit_code == 0, result.output
    assert "Started globo/node1 (pid: 367635)." in result.output
    assert "Error" not in result.output
    row = store.get_node("local", "globo", 1)
    assert row.pid == 367635 and row.node_state == "RUNNING"


def test_start_already_running_is_unchanged(spawn: Any, kills: Kills, monkeypatch: pytest.MonkeyPatch) -> None:
    store.set_pid(store.get_node("local", "globo", 1), 4312)
    kills.alive.add(4312)
    spawned: list[Any] = []
    monkeypatch.setattr(node_cmd.subprocess, "Popen", lambda *a, **k: spawned.append(a))

    result = runner.invoke(app, ["node", "start", "globo", "1"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "globo node1 is already running (pid 4312)" in result.output
    assert spawned == []


@pytest.mark.parametrize(
    ("code", "text"),
    [(-6, "killed by SIGABRT"), (-9, "killed by SIGKILL"), (-99, "killed by signal 99"),
     (0, "exited with code 0"), (75, "exited with code 75")],
)
def test_describe_exit(code: int, text: str) -> None:
    assert node_cmd.describe_exit(code) == text


def test_start_failure_hint_prefers_the_log_marker() -> None:
    # A NOT_DOWN abort that also exits 75 gets the more specific NOT_DOWN hint.
    hint = node_cmd.start_failure_hint(75, ["409 (NOT_DOWN): node org_node=1 is not DOWN"])
    assert hint is not None and hint.startswith("The control plane still has this node as UP.")
    assert node_cmd.start_failure_hint(3, ["nothing useful"]) is None
