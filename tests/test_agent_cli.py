"""`vl agent create|start|stop|list` (vl-agent-cli-spec.md §6).

No live stack: the identity provider and the control plane are respx stubs (an
unmatched request fails the test, it never leaves the process), the store and HOME
are temporary (conftest.py), and the agent `start` spawns is a stand-in package
``vl.agent`` put first on ``PYTHONPATH``, killed at the end. One test runs the real
agent, which stops at its configuration (exit 78) before any network call, against
an environment whose addresses are closed local ports.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from _helpers import add_service_account, add_user, kill_stand_in
from vl.agent import EXIT_CONFIG, EXIT_FAILED, EXIT_HOST_CHANGED, EXIT_NOPERM, EXIT_UNAVAILABLE
from vl.app import app
from vl.commands import agent as agent_cmd
from vl.lib import proc, store

runner = CliRunner()
IDP = "http://localhost:8080"
CP = "http://localhost:8082"
WIDE = {"COLUMNS": "200"}
JSON = {"VL_OUTPUT": "json", "COLUMNS": "200"}
ORG_DTO = {"id": "org-1", "name": "globo", "displayName": "Globo", "active": True,
           "createdAt": "2026-01-01T00:00:00Z"}


@pytest.fixture(autouse=True)
def orgs() -> None:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "2026-01-01T00:00:00Z")
    store.upsert_organization("local", "acme", "org-2", "Acme", True, "2026-01-01T00:00:00Z")


def admin(orgs_claim: list[dict[str, str]] | None = None, label: str = "ops") -> store.Identity:
    return add_user(label, orgs_claim if orgs_claim is not None else [{"orgId": "org-1", "role": "ORG_ADMIN"}])


def agent_row(org: str = "globo", label: str | None = None) -> store.Agent:
    sa = add_service_account(label or f"{org}-agent", org, role="NODE_AGENT")
    return store.add_agent("local", org, sa.id)


@pytest.fixture
def spawned() -> Iterator[list[int]]:
    """Every pid a test started; killed at the end, whatever happened."""
    pids: list[int] = []
    yield pids
    for agent in store.list_agents("local"):
        if agent.pid is not None:
            pids.append(agent.pid)
    for pid in pids:
        kill_stand_in(pid)


# A stand-in for the agent, run as `python -m vl.agent` exactly as `vl agent start` runs it.
FAKE_MAIN = textwrap.dedent(
    """
    import os, signal, sys, time
    with open(os.environ["FAKE_AGENT_ARGV"], "w") as f:
        f.write("\\n".join(sys.argv[1:]))
    mode = os.environ.get("FAKE_AGENT_MODE", "run")
    print("fake agent: starting", flush=True)
    if mode.startswith("exit:"):
        print("fake agent: boom", file=sys.stderr, flush=True)
        sys.exit(int(mode[5:]))
    if mode == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
        def term(signum, frame):
            print("fake agent: SIGTERM", flush=True)
            sys.exit(0)
        signal.signal(signal.SIGTERM, term)
    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(0.05)
    """
)


@pytest.fixture
def fake_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spawned: list[int]) -> Path:
    pkg = tmp_path / "fakepkg"
    (pkg / "vl" / "agent").mkdir(parents=True)
    (pkg / "vl" / "__init__.py").write_text("")
    (pkg / "vl" / "agent" / "__init__.py").write_text("")
    (pkg / "vl" / "agent" / "__main__.py").write_text(FAKE_MAIN)
    argv_file = tmp_path / "agent-argv"
    monkeypatch.setenv("PYTHONPATH", str(pkg))
    monkeypatch.setenv("FAKE_AGENT_ARGV", str(argv_file))
    monkeypatch.setattr(agent_cmd, "START_CHECK_SECONDS", 0.5)
    return argv_file


def wait_for(predicate: Any, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


# --- create --------------------------------------------------------------------------


def _created(display: str = "globo-agent") -> dict[str, Any]:
    return {"id": "sa-agent", "orgId": "org-1", "displayName": display, "role": "NODE_AGENT",
            "status": "PENDING", "keyVersion": 1, "bootstrapHash": "boot-1", "publicKey": None}


def _idp_routes(display: str = "globo-agent") -> respx.Route:
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(return_value=httpx.Response(200, json=ORG_DTO))
    route = respx.post(f"{IDP}/v1/service-accounts").mock(return_value=httpx.Response(201, json=_created(display)))
    respx.patch(f"{IDP}/v1/service-accounts/sa-agent/public-key").mock(
        return_value=httpx.Response(200, json={**_created(display), "status": "ACTIVE"}))
    return route


@respx.mock
def test_create_makes_the_account_the_row_and_the_directory(isolated_home: Path) -> None:
    admin()
    create = _idp_routes()

    result = runner.invoke(app, ["agent", "create", "globo"], env=WIDE)

    assert result.exit_code == 0, result.output
    body = json.loads(create.calls.last.request.content)
    assert (body["displayName"], body["role"], body["orgId"]) == ("globo-agent", "NODE_AGENT", "org-1")
    agent = store.get_agent("local", "globo")
    assert (agent.state, agent.pid) == ("NEW", None)
    identity = store.get_identity_by_id(agent.svc_acct_id)
    assert identity.label == "globo-agent" and identity.kind == "SERVICE_ACCOUNT"
    svc = store.get_svc_acct(identity.id)
    assert svc is not None and (svc.role, svc.org_name) == ("NODE_AGENT", "globo")
    assert (isolated_home / "orgs" / "globo" / "agent" / "data").is_dir()
    assert not (isolated_home / "orgs" / "globo" / "agent" / "bin").exists()  # nothing copied
    assert "vl agent start globo" in result.output
    assert "Client secret" not in result.output  # stored, never needed elsewhere


@respx.mock
def test_create_with_a_name(isolated_home: Path) -> None:
    admin()
    create = _idp_routes("dev.box_1")
    result = runner.invoke(app, ["agent", "create", "globo", "--name", "dev.box_1"], env=WIDE)
    assert result.exit_code == 0, result.output
    assert json.loads(create.calls.last.request.content)["displayName"] == "dev.box_1"
    assert store.get_identity_by_id(store.get_agent("local", "globo").svc_acct_id).label == "dev.box_1"


@pytest.mark.parametrize("claims, why", [
    ([{"orgId": "org-9", "role": "PLATFORM_ADMIN"}], "PLATFORM_ADMIN is not enough"),
    ([{"orgId": "org-1", "role": "USER"}], "not an admin of 'globo'"),
    ([{"orgId": "org-2", "role": "ORG_ADMIN"}], "not an admin of 'globo'"),
])
@respx.mock(assert_all_called=False)
def test_create_needs_org_admin_of_that_org_and_calls_nothing(claims: list[dict[str, str]], why: str,
                                                              isolated_home: Path) -> None:
    admin(claims)
    create = _idp_routes()
    result = runner.invoke(app, ["agent", "create", "globo"], env=WIDE)
    assert result.exit_code == 1 and why in result.output, result.output
    assert create.call_count == 0 and store.list_agents("local") == []
    assert not (isolated_home / "orgs" / "globo" / "agent").exists()


@respx.mock(assert_all_called=False)
def test_create_refuses_a_second_agent_before_any_call() -> None:
    admin()
    agent_row()
    create = _idp_routes()
    result = runner.invoke(app, ["agent", "create", "globo", "--name", "other"], env=WIDE)
    assert result.exit_code == 1 and "already has an agent" in result.output
    assert create.call_count == 0


@respx.mock(assert_all_called=False)
def test_create_refuses_a_name_the_control_plane_would_refuse() -> None:
    admin()
    create = _idp_routes()
    result = runner.invoke(app, ["agent", "create", "globo", "--name", "dev box"], env=WIDE)
    assert result.exit_code == 1 and "must be 1 to 64" in result.output
    assert create.call_count == 0


@respx.mock(assert_all_called=False)
def test_create_as_a_service_account_is_refused() -> None:
    add_service_account("robot", "globo", role="SYSTEM")
    store.set_default_identity("local", "robot")
    create = _idp_routes()
    result = runner.invoke(app, ["agent", "create", "globo"], env=WIDE)
    assert result.exit_code == 1 and "service account" in result.output
    assert create.call_count == 0


# --- start ---------------------------------------------------------------------------


def test_start_spawns_the_agent_detached_and_records_it(fake_agent: Path, isolated_home: Path) -> None:
    admin()
    agent_row()

    result = runner.invoke(app, ["agent", "start", "globo"], env=WIDE)

    assert result.exit_code == 0, result.output
    agent = store.get_agent("local", "globo")
    assert agent.state == "RUNNING" and agent.pid is not None
    assert fake_agent.read_text().splitlines() == ["--as", "globo-agent", "--env", "local"]
    args = proc.cmdline(agent.pid)
    assert args is not None and args[:3] == [sys.executable, "-m", "vl.agent"]
    assert os.getsid(agent.pid) == agent.pid != os.getsid(0)  # its own session
    assert proc.is_agent_process(agent.pid, "globo-agent")
    log = isolated_home / "tmp" / "globo-agent.out"
    assert wait_for(lambda: "fake agent: starting" in log.read_text())
    assert f"pid {agent.pid}" in result.output and str(log) in result.output


def test_start_passes_the_environments_node_host(fake_agent: Path) -> None:
    admin()
    agent_row()
    store.update_environment("local", node_host="10.1.2.3")
    result = runner.invoke(app, ["agent", "start", "globo"], env=WIDE)
    assert result.exit_code == 0, result.output
    assert fake_agent.read_text().splitlines() == ["--as", "globo-agent", "--env", "local", "--host", "10.1.2.3"]
    assert "10.1.2.3" in result.output


def test_the_argv_without_and_with_a_node_host() -> None:
    env = store.get_environment("local")
    assert agent_cmd.agent_argv(env, "globo-agent") == [sys.executable, "-m", "vl.agent", "--as", "globo-agent",
                                                        "--env", "local"]
    store.update_environment("local", node_host="h1")
    assert agent_cmd.agent_argv(store.get_environment("local"), "x")[-2:] == ["--host", "h1"]


@pytest.mark.parametrize("code, hint", [
    (EXIT_CONFIG, "configuration or identity is invalid"),
    (EXIT_NOPERM, "Authentication was refused"),
    (EXIT_UNAVAILABLE, "could not be reached"),
    (EXIT_HOST_CHANGED, "address changed"),
    (EXIT_FAILED, "Startup failed"),
])
def test_an_early_exit_is_an_error_with_the_hint_and_the_log_tail(code: int, hint: str, fake_agent: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    admin()
    agent_row()
    monkeypatch.setenv("FAKE_AGENT_MODE", f"exit:{code}")
    result = runner.invoke(app, ["agent", "start", "globo"], env=WIDE)
    assert result.exit_code == 1
    assert f"exited with code {code}" in result.output and hint in result.output
    assert "---- Log tail ----" in result.output and "fake agent: boom" in result.output
    agent = store.get_agent("local", "globo")
    assert (agent.state, agent.pid) == ("STOPPED", None)


def test_the_real_agent_with_a_bad_setting_exits_78_and_says_so(isolated_home: Path, spawned: list[int],
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    # Closed local ports: nothing here can reach a real service, and the agent stops
    # at its configuration, before authenticating.
    closed = "http://127.0.0.1:9"
    store.update_environment("local", closed, closed, closed, f"{closed}/t", closed, "127.0.0.1:9")
    admin()
    agent_row()
    props = isolated_home / "orgs" / "globo" / "agent" / "agent.properties"
    props.parent.mkdir(parents=True)
    props.write_text("agent.heartbeat.interval-seconds=often\n")
    monkeypatch.setattr(agent_cmd, "START_CHECK_SECONDS", 5.0)

    result = runner.invoke(app, ["agent", "start", "globo"], env=WIDE)

    assert result.exit_code == 1, result.output
    assert "exited with code 78" in result.output and "configuration or identity" in result.output
    assert "agent.heartbeat.interval-seconds must be a number" in result.output  # from the log tail
    assert store.get_agent("local", "globo").state == "STOPPED"


def test_start_without_the_agent_extra_says_install_it(fake_agent: Path, monkeypatch: pytest.MonkeyPatch,
                                                       isolated_home: Path) -> None:
    admin()
    agent_row()
    monkeypatch.setattr(agent_cmd, "kafka_installed", lambda: False)
    result = runner.invoke(app, ["agent", "start", "globo"], env=WIDE)
    assert result.exit_code == 1 and "vl[agent]" in result.output
    assert store.get_agent("local", "globo").state == "NEW"
    assert not fake_agent.exists() and not (isolated_home / "tmp" / "globo-agent.out").exists()


def test_start_when_already_running_starts_nothing(fake_agent: Path) -> None:
    admin()
    agent_row()
    assert runner.invoke(app, ["agent", "start", "globo"], env=WIDE).exit_code == 0
    first = store.get_agent("local", "globo").pid
    fake_agent.unlink()
    again = runner.invoke(app, ["agent", "start", "globo"], env=WIDE)
    assert again.exit_code == 0 and "already running" in again.output
    assert store.get_agent("local", "globo").pid == first and not fake_agent.exists()


def test_start_needs_an_operator(fake_agent: Path) -> None:
    admin([{"orgId": "org-1", "role": "USER"}])
    agent_row()
    result = runner.invoke(app, ["agent", "start", "globo"], env=WIDE)
    assert result.exit_code == 1 and "not an admin" in result.output
    assert not fake_agent.exists() and store.get_agent("local", "globo").state == "NEW"


def test_start_with_no_agent_points_at_create() -> None:
    admin()
    result = runner.invoke(app, ["agent", "start", "globo"], env=WIDE)
    assert result.exit_code == 1 and "vl agent create globo" in result.output


def test_start_names_the_org_it_needs() -> None:
    result = runner.invoke(app, ["agent", "start"], env=WIDE)
    assert result.exit_code == 2


# --- stop ----------------------------------------------------------------------------


def test_stop_terminates_the_agent_and_records_it(fake_agent: Path, isolated_home: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    admin()
    agent_row()
    assert runner.invoke(app, ["agent", "start", "globo"], env=WIDE).exit_code == 0
    pid = store.get_agent("local", "globo").pid
    assert pid is not None
    sent: list[tuple[int, int]] = []
    real_kill = os.kill
    monkeypatch.setattr(agent_cmd.os, "kill", lambda p, s: (sent.append((p, s)), real_kill(p, s))[1])

    result = runner.invoke(app, ["agent", "stop", "globo"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert sent == [(pid, signal.SIGTERM)]  # the agent's own pid, nothing else
    assert not proc.is_alive(pid)
    agent = store.get_agent("local", "globo")
    assert (agent.state, agent.pid) == ("STOPPED", None)
    assert "stopped" in result.output and "nodes were not touched" in result.output
    assert wait_for(lambda: "fake agent: SIGTERM" in (isolated_home / "tmp" / "globo-agent.out").read_text())


def test_stop_escalates_to_sigkill(fake_agent: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    admin()
    agent_row()
    monkeypatch.setenv("FAKE_AGENT_MODE", "ignore-term")
    assert runner.invoke(app, ["agent", "start", "globo"], env=WIDE).exit_code == 0
    pid = store.get_agent("local", "globo").pid
    assert pid is not None
    monkeypatch.setattr(agent_cmd, "STOP_TERM_SECONDS", 0.5)

    result = runner.invoke(app, ["agent", "stop", "globo"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "SIGKILL" in result.output and "killed" in result.output
    assert not proc.is_alive(pid)
    assert store.get_agent("local", "globo").state == "STOPPED"


def test_stop_never_touches_the_nodes(fake_agent: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    admin()
    agent_row()
    sa = add_service_account("globo-1", "globo")
    node = store.set_pid(store.add_node("local", "globo", 1, sa.id, 7001), 999_999)
    assert runner.invoke(app, ["agent", "start", "globo"], env=WIDE).exit_code == 0
    from vl.lib import nodeops
    for name in ("stop_node", "stop_nodes_to_down", "shutdown_node", "reset_node"):
        monkeypatch.setattr(nodeops, name, lambda *a, **k: pytest.fail("a node was touched"))
    assert runner.invoke(app, ["agent", "stop", "globo"], env=WIDE).exit_code == 0
    assert store.get_node("local", "globo", 1) == node


def test_stop_with_a_dead_pid_records_stopped_and_signals_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    admin()
    agent = agent_row()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    store.set_agent_pid(agent, dead.pid)
    monkeypatch.setattr(agent_cmd.os, "kill", lambda p, s: pytest.fail("signalled"))
    result = runner.invoke(app, ["agent", "stop", "globo"], env=WIDE)
    assert result.exit_code == 0 and "was not running" in result.output
    assert store.get_agent("local", "globo").state == "STOPPED"


def test_stop_never_signals_a_reused_pid(spawned: list[int]) -> None:
    admin()
    agent = agent_row()
    other = subprocess.Popen(["sleep", "30"])
    spawned.append(other.pid)
    store.set_agent_pid(agent, other.pid)  # alive, but not this agent
    result = runner.invoke(app, ["agent", "stop", "globo"], env=WIDE)
    assert result.exit_code == 0
    assert other.poll() is None  # still running
    assert store.get_agent("local", "globo").state == "STOPPED"


def test_stop_a_new_agent_says_it_is_not_running() -> None:
    admin()
    agent_row()
    result = runner.invoke(app, ["agent", "stop", "globo"], env=WIDE)
    assert result.exit_code == 0 and "is not running" in result.output
    assert store.get_agent("local", "globo").state == "NEW"


# --- list ----------------------------------------------------------------------------


def running_agent(org: str, label: str, spawned: list[int]) -> store.Agent:
    """A row whose pid is a live process that looks like the agent for ``label``."""
    agent = agent_row(org, label)
    stand_in = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "-m", "vl.agent",
                                 "--as", label])
    spawned.append(stand_in.pid)
    assert wait_for(lambda: proc.is_agent_process(stand_in.pid, label))
    return store.set_agent_pid(agent, stand_in.pid)


def cp_agent(org_id: str, name: str, state: str = "ALIVE", nodes: int = 2, host: str = "192.168.8.2",
             last_seen: str | None = "2026-11-14T12:00:00Z") -> dict[str, Any]:
    return {"id": f"agent-{name}", "name": name, "orgId": org_id, "host": host, "agentVersion": "0.1.0",
            "startedAt": None, "lastSeenAt": last_seen, "createdAt": "2026-11-14T11:00:00Z",
            "state": state, "nodeCount": nodes}


def listing(*args: str, env: dict[str, str] = JSON) -> Any:
    return runner.invoke(app, ["agent", "list", *args], env=env)


@respx.mock
def test_list_joins_the_control_planes_view(spawned: list[int]) -> None:
    admin([{"orgId": "org-1", "role": "ORG_ADMIN"}, {"orgId": "org-2", "role": "ORG_ADMIN"}])
    running_agent("globo", "globo-agent", spawned)
    agent_row("acme")
    route = respx.get(f"{CP}/v1/agents").mock(return_value=httpx.Response(200, json={"items": [
        cp_agent("org-1", "globo-agent"), cp_agent("org-2", "somewhere-else", host="10.0.0.9")]}))

    result = listing()

    assert result.exit_code == 0, result.output
    rows = {r["org"]: r for r in json.loads(result.stdout)}
    assert rows["globo"]["state"] == "RUNNING" and rows["globo"]["cp_state"] == "ALIVE"
    assert (rows["globo"]["host"], rows["globo"]["nodes"], rows["globo"]["attention"]) == ("192.168.8.2", 2, None)
    assert rows["acme"]["state"] == "NEW" and rows["acme"]["cp_state"] == "UNREGISTERED"
    assert route.calls.last.request.url.params["limit"] == "500"


@respx.mock
def test_list_highlights_the_host_mismatch_symptoms(spawned: list[int]) -> None:
    admin([{"orgId": "org-1", "role": "ORG_ADMIN"}, {"orgId": "org-2", "role": "ORG_ADMIN"}])
    running_agent("globo", "globo-agent", spawned)
    running_agent("acme", "acme-agent", spawned)
    respx.get(f"{CP}/v1/agents").mock(return_value=httpx.Response(200, json={"items": [
        cp_agent("org-1", "globo-agent", state="STALE"), cp_agent("org-2", "acme-agent", nodes=0)]}))

    rows = {r["org"]: r for r in json.loads(listing().stdout)}
    assert "STALE at the control plane" in rows["globo"]["attention"]
    assert "no node of the org is registered with the agent's host" in rows["acme"]["attention"]

    table = listing(env=WIDE)
    assert table.exit_code == 0
    assert "globo: running here, but STALE" in table.output
    assert "acme: no node of the org" in table.output


@respx.mock
def test_a_stale_alive_agent_that_is_stopped_here_is_not_a_symptom() -> None:
    admin()
    agent_row()
    respx.get(f"{CP}/v1/agents").mock(return_value=httpx.Response(200, json={"items": [
        cp_agent("org-1", "globo-agent", state="STALE")]}))
    [row] = json.loads(listing().stdout)
    assert (row["state"], row["cp_state"], row["attention"]) == ("NEW", "STALE", None)


@respx.mock(assert_all_called=False)
def test_list_without_an_identity_shows_the_local_columns_and_says_why() -> None:
    agent_row()
    route = respx.get(f"{CP}/v1/agents")
    result = listing(env=WIDE)
    assert result.exit_code == 0, result.output
    assert "globo-agent" in result.output and "Control plane columns not shown: no operator identity" in result.output
    assert route.call_count == 0
    [row] = json.loads(listing().stdout)
    assert (row["cp_state"], row["host"], row["nodes"], row["last_seen"]) == ("-", "-", "-", "-")


@respx.mock
def test_list_with_the_control_plane_unreachable_shows_the_local_columns() -> None:
    admin()
    agent_row()
    respx.get(f"{CP}/v1/agents").mock(side_effect=httpx.ConnectError("refused"))
    result = listing(env=WIDE)
    assert result.exit_code == 0, result.output
    assert "not shown: it could not be reached" in result.output and "globo-agent" in result.output


@respx.mock
def test_list_with_a_refusal_shows_the_local_columns_and_the_refusal() -> None:
    admin()
    agent_row()
    respx.get(f"{CP}/v1/agents").mock(return_value=httpx.Response(403, json={"message": "Forbidden"}))
    result = listing(env=WIDE)
    assert result.exit_code == 0 and "not shown: it refused the listing" in result.output


@respx.mock(assert_all_called=False)
def test_list_as_a_service_account_shows_the_local_columns() -> None:
    agent_row()
    add_service_account("robot", "globo", role="SYSTEM")
    route = respx.get(f"{CP}/v1/agents")
    result = listing("--as", "robot", env=WIDE)
    assert result.exit_code == 0 and "is a service account" in result.output
    assert route.call_count == 0


@respx.mock
def test_list_repairs_a_running_row_whose_process_is_gone() -> None:
    admin()
    agent = agent_row()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    store.set_agent_pid(agent, dead.pid)
    respx.get(f"{CP}/v1/agents").mock(return_value=httpx.Response(200, json={"items": []}))
    [row] = json.loads(listing().stdout)
    assert row["state"] == "STOPPED" and row["pid"] is None
    assert store.get_agent("local", "globo").state == "STOPPED"


@respx.mock
def test_list_org_narrows_it_and_asks_for_that_org(spawned: list[int]) -> None:
    admin([{"orgId": "org-1", "role": "ORG_ADMIN"}, {"orgId": "org-2", "role": "ORG_ADMIN"}])
    agent_row("globo")
    agent_row("acme")
    route = respx.get(f"{CP}/v1/agents").mock(return_value=httpx.Response(200, json={"items": []}))
    rows = json.loads(listing("acme").stdout)
    assert [r["org"] for r in rows] == ["acme"]
    assert route.calls.last.request.url.params["orgId"] == "org-2"


@respx.mock
def test_list_remote_shows_every_agent_the_control_plane_shows() -> None:
    admin([{"orgId": "org-1", "role": "ORG_ADMIN"}, {"orgId": "org-2", "role": "ORG_ADMIN"}])
    agent_row()
    respx.get(f"{CP}/v1/agents").mock(return_value=httpx.Response(200, json={"items": [
        cp_agent("org-1", "lab-2", host="192.168.8.9"), cp_agent("org-2", "acme-box", nodes=0)]}))
    rows = json.loads(listing("--remote").stdout)
    assert [(r["org"], r["agent"], r["host"]) for r in rows] == [("globo", "lab-2", "192.168.8.9"),
                                                               ("acme", "acme-box", "192.168.8.2")]
    assert "state" not in rows[0] and "pid" not in rows[0]  # no local columns
    assert listing("--remote", env=WIDE).exit_code == 0


@respx.mock
def test_list_remote_without_the_control_plane_is_an_error() -> None:
    admin()
    respx.get(f"{CP}/v1/agents").mock(side_effect=httpx.ConnectError("refused"))
    result = listing("--remote", env=WIDE)
    assert result.exit_code == 1


def test_list_with_no_agents_points_at_create() -> None:
    result = listing(env=WIDE)
    assert result.exit_code == 0 and "vl agent create" in result.output


def test_an_agent_process_is_recognised_by_its_label(spawned: list[int]) -> None:
    agent = running_agent("globo", "globo-agent", spawned)
    assert proc.is_agent_process(agent.pid, "globo-agent")
    assert not proc.is_agent_process(agent.pid, "acme-agent")  # another org's agent: not ours
    assert not proc.is_agent_process(os.getpid(), "globo-agent")  # alive, but not an agent
