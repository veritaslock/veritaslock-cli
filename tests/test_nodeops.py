"""The shared node operations (vl-agent-cli-spec.md §4, §12).

Each outcome; emit gets every progress line and nothing prints; nothing prompts
or checks a role; the lock cases (two starts never give two live nodes; the
repair never clobbers a start in progress). Everything runs under the test's
temporary HOME and store; the only real "nodes" are stand-in shell scripts.
"""

from __future__ import annotations

import ast
import builtins
import inspect
import os
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer

from _helpers import kill_stand_in
from vl.lib import nodelock, nodeops, output, store
from vl.lib.nodeops import Outcome

SRC = Path(__file__).resolve().parent.parent / "src" / "vl" / "lib" / "nodeops.py"
# The machine fixture replaces these; the real lock holder needs the real ones.
_REAL_POPEN = subprocess.Popen
_REAL_KILL = os.kill


class Machine:
    """The processes and the control plane a node operation sees, faked in-process."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.alive: set[int] = set()
        self.stubborn: set[int] = set()
        self.immortal: set[int] = set()
        self.kills: list[tuple[int, int]] = []
        self.spawned: list[int] = []
        self.exit: tuple[int, str] | None = None  # (returncode, log) for the next spawn
        self.cp: dict[int, list[str]] = {}  # org_node -> states, the last repeating
        self.sleeps: list[float] = []

    def popen(self, args: list[str], *, stdout: Any, **_: Any) -> Any:
        pid = 6000 + len(self.spawned)
        self.spawned.append(pid)
        rc, log = self.exit if self.exit else (None, "started\n")
        stdout.write(log)
        if rc is None:
            self.alive.add(pid)
        return SimpleNamespace(pid=pid, poll=lambda: rc)

    def kill(self, pid: int, sig: int) -> None:
        self.kills.append((pid, sig))
        if sig == signal.SIGTERM and pid not in self.stubborn:
            self.alive.discard(pid)
        if sig == signal.SIGKILL and pid not in self.immortal:
            self.alive.discard(pid)

    def fetch(self, environment: store.Environment, node: store.Node) -> dict[str, Any]:
        states = self.cp.setdefault(node.org_node, ["DOWN"])
        return {"state": states.pop(0) if len(states) > 1 else states[0]}


@pytest.fixture
def machine(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Machine:
    m = Machine(isolated_home)
    monkeypatch.setattr(nodeops.subprocess, "Popen", m.popen)
    monkeypatch.setattr(nodeops.os, "kill", m.kill)
    monkeypatch.setattr(nodeops.proc, "is_our_process", lambda pid, exe, **_: pid in m.alive)
    monkeypatch.setattr(nodeops.time, "sleep", m.sleeps.append)
    monkeypatch.setattr(nodeops, "fetch_node_by_id", m.fetch)
    monkeypatch.setattr(nodeops, "populate_node_dir", lambda env, org, n, node: nodeops.node_root(org, n))
    store.ensure_local_environment_seeded()
    for org, org_id in (("globo", "org-1"), ("acme", "org-2")):
        store.upsert_organization("local", org, org_id, org.title(), True, "t0")
    return m


def _node(org: str = "globo", n: int = 1, *, pid: int | None = None, state: str = "NEW") -> store.Node:
    ident = store.add_identity("local", "SERVICE_ACCOUNT", f"client-{org}-{n}", f"{org}-{n}", f"{org}-{n}")
    store.set_svc_acct(ident.id, "local", org, f"client-{org}-{n}", "secret", role="NODE")
    row = store.add_node("local", org, n, ident.id)
    root = nodeops.node_root(org, n)
    for sub in ("bin", "data", "snapshots"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "data" / "chain.db").write_text("blocks")
    (root / "snapshots" / "s.tgz").write_text("snap")
    if state == "RUNNING":
        assert pid is not None
        row = store.set_pid(row, pid)
    elif state == "STOPPED":
        row = store.set_node_stopped(store.set_pid(row, 1))
    return row


def _env() -> store.Environment:
    return store.get_environment("local")


class Lines(list[str]):
    """An emit that keeps what it was given."""

    def __call__(self, text: str) -> None:
        self.append(text)


# --------------------------------------------------------------------------- #
# each outcome
# --------------------------------------------------------------------------- #


def test_start_started_with_its_pid(machine: Machine) -> None:
    _node()
    emit = Lines()
    result = nodeops.start_node(_env(), "globo", 1, emit)
    assert result == nodeops.NodeResult(Outcome.STARTED, "pid 6000", pid=6000)
    assert emit == ["Started globo/node1 (pid: 6000)."]
    assert store.get_node("local", "globo", 1).pid == 6000


def test_start_already_running(machine: Machine) -> None:
    _node(pid=4312, state="RUNNING")
    machine.alive.add(4312)
    emit = Lines()
    result = nodeops.start_node(_env(), "globo", 1, emit)
    assert (result.outcome, result.pid) == (Outcome.ALREADY_RUNNING, 4312)
    assert emit == ["globo node1 is already running (pid 4312)"]
    assert machine.spawned == []


@pytest.mark.parametrize(
    ("code", "hint"),
    [
        (69, "The control plane still has this node as UP. Wait about 30 seconds for its heartbeat timeout"),
        (75, "The node asked to be started again later"),
        (74, "The node stopped because of a storage failure."),
        (-6, None),
        (1, None),
    ],
)
def test_start_failed_carries_the_exit_code_and_the_hint(machine: Machine, code: int, hint: str | None) -> None:
    _node()
    machine.exit = (code, "INFO starting\n")
    emit = Lines()

    result = nodeops.start_node(_env(), "globo", 1, emit)

    assert (result.outcome, result.exit_code, result.pid) == (Outcome.FAILED, code, None)
    assert result.detail.startswith(f"exited right after starting ({nodeops.describe_exit(code)})")
    if hint is None:
        assert result.detail == nodeops.start_failure_reason(code)
    else:
        assert result.detail == f"{nodeops.start_failure_reason(code)}. {nodeops.start_failure_hint(code, [])}"
        assert hint in result.detail
    assert emit == []  # a failure is the result, not a progress line
    row = store.get_node("local", "globo", 1)
    assert (row.node_state, row.pid) == ("STOPPED", None)


def test_start_failed_hint_from_the_log_marker(machine: Machine) -> None:
    _node()
    machine.exit = (-6, "ERROR registration failed: 409 (NOT_DOWN)\n")
    result = nodeops.start_node(_env(), "globo", 1, Lines())
    assert result.outcome is Outcome.FAILED and "A kill -9 gives a node no chance" in result.detail


def test_stop_stopped(machine: Machine) -> None:
    _node(pid=4312, state="RUNNING")
    machine.alive.add(4312)
    emit = Lines()
    result = nodeops.stop_node(_env(), "globo", 1, emit)
    assert result == nodeops.NodeResult(Outcome.STOPPED, "pid 4312", pid=4312)
    assert emit == ["Stopped globo/node1 (pid: 4312)."]
    assert machine.kills == [(4312, signal.SIGTERM)]


def test_stop_stopped_after_sigkill(machine: Machine) -> None:
    _node(pid=4312, state="RUNNING")
    machine.alive.add(4312)
    machine.stubborn.add(4312)
    emit = Lines()
    result = nodeops.stop_node(_env(), "globo", 1, emit)
    assert result.outcome is Outcome.STOPPED
    assert emit == ["Terminated globo/node1 (pid: 4312)."]


def test_stop_already_stopped_without_a_pid(machine: Machine) -> None:
    _node(state="STOPPED")
    emit = Lines()
    result = nodeops.stop_node(_env(), "globo", 1, emit)
    assert result == nodeops.NodeResult(Outcome.ALREADY_STOPPED, "not running")
    assert emit == ["globo/node1 is not running."]


def test_stop_already_stopped_with_a_stale_pid_never_signals_it(machine: Machine) -> None:
    _node(pid=4312, state="RUNNING")  # not alive: a stale pid
    emit = Lines()
    result = nodeops.stop_node(_env(), "globo", 1, emit)
    assert result.outcome is Outcome.ALREADY_STOPPED and "stale pid 4312" in result.detail
    assert emit == ["done"] and machine.kills == []
    assert store.get_node("local", "globo", 1).node_state == "STOPPED"


def test_stop_failed_when_it_will_not_die(machine: Machine) -> None:
    _node(pid=4312, state="RUNNING")
    machine.alive.add(4312)
    machine.stubborn.add(4312)
    machine.immortal.add(4312)
    emit = Lines()
    result = nodeops.stop_node(_env(), "globo", 1, emit)
    assert (result.outcome, result.pid) == (Outcome.FAILED, 4312)
    assert emit == ["Failure to shutdown globo/node1 (pid: 4312)."]
    assert store.get_node("local", "globo", 1).node_state == "RUNNING"


@pytest.mark.parametrize("include_snapshots", [False, True])
def test_reset_reset(machine: Machine, include_snapshots: bool) -> None:
    _node(state="STOPPED")
    emit = Lines()
    result = nodeops.reset_node(_env(), "globo", 1, emit, include_snapshots=include_snapshots)
    root = nodeops.node_root("globo", 1)
    assert result.outcome is Outcome.RESET
    assert not (root / "data" / "chain.db").exists()
    assert (root / "snapshots" / "s.tgz").exists() is not include_snapshots
    what = "data and snapshots directories" if include_snapshots else "data directory"
    assert emit == [f"Deleted globo/node1's {what}."]


def test_reset_skipped_up(machine: Machine) -> None:
    _node(state="STOPPED")
    machine.cp[1] = ["UP"]
    emit = Lines()
    result = nodeops.reset_node(_env(), "globo", 1, emit)
    assert result == nodeops.NodeResult(Outcome.SKIPPED_UP, "still reported UP by the control plane")
    assert emit == ["globo/node1 is still reported UP by the control plane — stop it first or try again."]
    assert (nodeops.node_root("globo", 1) / "data" / "chain.db").exists()


def test_reset_of_a_new_node_makes_no_control_plane_check(machine: Machine) -> None:
    _node()
    machine.cp[1] = ["UP"]  # never asked
    assert nodeops.reset_node(_env(), "globo", 1, Lines()).outcome is Outcome.RESET
    assert machine.cp[1] == ["UP"]


def _hold_lock_in_another_process(root: Path) -> subprocess.Popen[str]:
    holder = _REAL_POPEN(
        [sys.executable, "-c", textwrap.dedent(
            """
            import sys, time
            from pathlib import Path
            from vl.lib import nodelock
            with nodelock.node_lock(Path(sys.argv[1])):
                print("locked", flush=True)
                time.sleep(30)
            """
        ), str(root)],
        stdout=subprocess.PIPE, text=True,
    )
    assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
    return holder


@pytest.mark.parametrize("verb", ["start", "stop", "reset"])
def test_a_busy_node_is_failed_another_operation_is_in_progress(machine: Machine, verb: str) -> None:
    _node(pid=4312, state="RUNNING")
    machine.alive.add(4312)
    holder = _hold_lock_in_another_process(nodeops.node_root("globo", 1))
    try:
        operation = getattr(nodeops, f"{verb}_node")
        emit = Lines()
        result = operation(_env(), "globo", 1, emit, lock_wait=0.3)
    finally:
        _REAL_KILL(holder.pid, signal.SIGKILL)
        holder.wait()
    assert result == nodeops.NodeResult(Outcome.FAILED, "another operation is in progress")
    assert emit == [] and machine.kills == [] and machine.spawned == []


def test_the_operations_wait_10_seconds_by_default() -> None:
    for operation in (nodeops.start_node, nodeops.stop_node, nodeops.reset_node, nodeops.stop_nodes_to_down):
        assert inspect.signature(operation).parameters["lock_wait"].default == 10


# --------------------------------------------------------------------------- #
# stop_nodes_to_down: stop, then wait for DOWN, descending, with the pauses
# --------------------------------------------------------------------------- #


def test_stop_nodes_to_down_stops_descending_and_waits_for_down(machine: Machine) -> None:
    nodes = [_node("globo", n, pid=4310 + n, state="RUNNING") for n in (1, 2, 3)]
    machine.alive.update({4311, 4312, 4313})
    machine.cp = {1: ["UP", "DOWN"], 2: ["DOWN"], 3: ["UP", "UP", "DOWN"]}
    emit = Lines()

    results = nodeops.stop_nodes_to_down(_env(), nodes, emit)

    assert [(n.org_node, r.outcome) for n, r in results] == [
        (3, Outcome.STOPPED), (2, Outcome.STOPPED), (1, Outcome.STOPPED)
    ]
    assert emit == [f"Stopped globo/node{n} (pid: {4310 + n})." for n in (3, 2, 1)]
    assert [n.org_node for n in nodes] == [1, 2, 3]  # the caller's list is not reordered
    # node3: 3 polls; pause; node2: 1 poll; GLOBO's last node: 2 s; node1: 2 polls
    assert machine.sleeps == [0.25] * 3 + [0.5, 0.25, 2, 0.25, 0.25]


def test_stop_nodes_to_down_pauses_half_a_second_outside_globo(machine: Machine) -> None:
    nodes = [_node("acme", n, state="STOPPED") for n in (1, 2)]
    nodeops.stop_nodes_to_down(_env(), nodes, Lines())
    assert machine.sleeps == [0.25, 0.5, 0.25]


def test_stop_nodes_to_down_still_up_is_failed_and_the_rest_go_on(machine: Machine) -> None:
    nodes = [_node("acme", n, pid=4320 + n, state="RUNNING") for n in (1, 2)]
    machine.alive.update({4321, 4322})
    machine.cp = {2: ["UP"]}  # never DOWN
    emit = Lines()

    results = nodeops.stop_nodes_to_down(_env(), nodes, emit, polls=4)

    assert [(n.org_node, r.outcome, r.detail) for n, r in results] == [
        (2, Outcome.FAILED, "still reported UP by the control plane"),
        (1, Outcome.STOPPED, "pid 4321"),
    ]
    assert "Warning: acme-node2 is still reported UP by the control plane." in emit
    assert machine.sleeps.count(0.25) == 4 + 1


def test_stop_nodes_to_down_an_exception_is_that_nodes_failure(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    nodes = [_node("acme", n, state="STOPPED") for n in (1, 2)]

    def fetch(environment: store.Environment, node: store.Node) -> dict[str, Any]:
        if node.org_node == 2:
            raise store.StoreError("no token")
        return {"state": "DOWN"}

    monkeypatch.setattr(nodeops, "fetch_node_by_id", fetch)
    emit = Lines()
    results = nodeops.stop_nodes_to_down(_env(), nodes, emit)
    assert [(n.org_node, r.outcome, r.detail) for n, r in results] == [
        (2, Outcome.FAILED, "no token"), (1, Outcome.ALREADY_STOPPED, "not running")
    ]
    assert "Warning: no token" in emit


# --------------------------------------------------------------------------- #
# emit, print, prompts, roles
# --------------------------------------------------------------------------- #


def _forbid(monkeypatch: pytest.MonkeyPatch, *targets: tuple[Any, str]) -> list[str]:
    called: list[str] = []
    for owner, name in targets:
        def forbidden(*a: Any, _name: str = name, **kw: Any) -> Any:
            called.append(_name)
            raise AssertionError(f"{_name} called by a shared operation")
        monkeypatch.setattr(owner, name, forbidden)
    return called


def _run_every_operation(machine: Machine) -> Lines:
    emit = Lines()
    env = _env()
    _node("globo", 1)
    _node("globo", 2, pid=4312, state="RUNNING")
    machine.alive.add(4312)
    _node("globo", 3, state="STOPPED")
    machine.cp[3] = ["UP"]
    nodeops.start_node(env, "globo", 1, emit)
    nodeops.start_node(env, "globo", 2, emit)
    nodeops.stop_node(env, "globo", 2, emit)
    nodeops.stop_node(env, "globo", 2, emit)
    nodeops.reset_node(env, "globo", 3, emit)
    nodeops.reset_node(env, "globo", 2, emit, include_snapshots=True)
    nodeops.stop_nodes_to_down(env, store.get_nodes(env, store.get_organization("local", "globo")), emit)
    nodeops.repair(store.set_pid(store.get_node("local", "globo", 3), 9999))
    return emit


def test_emit_gets_every_progress_line_and_nothing_prints(machine: Machine, monkeypatch: pytest.MonkeyPatch,
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    called = _forbid(monkeypatch, (builtins, "print"), (output.console, "print"),
                     (output.err_console, "print"), (output, "note"), (output, "warn"))

    emit = _run_every_operation(machine)

    assert called == []
    assert capsys.readouterr() == ("", "")
    assert emit == [
        "Started globo/node1 (pid: 6000).",
        "globo node2 is already running (pid 4312)",
        "Stopped globo/node2 (pid: 4312).",
        "globo/node2 is not running.",
        "globo/node3 is still reported UP by the control plane — stop it first or try again.",
        "Deleted globo/node2's data and snapshots directories.",
        # stop_nodes_to_down, descending: node3 never ran and the CP keeps it UP
        "globo/node3 is not running.",
        "Warning: globo-node3 is still reported UP by the control plane.",
        "globo/node2 is not running.",
        "Stopped globo/node1 (pid: 6000).",
    ]


def test_no_function_prompts(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True, readline=lambda: "y\n"))
    called = _forbid(monkeypatch, (typer, "confirm"), (typer, "prompt"), (builtins, "input"))

    _run_every_operation(machine)

    assert called == []
    for name in ("start_node", "stop_node", "reset_node", "stop_nodes_to_down", "repair"):
        params = inspect.signature(getattr(nodeops, name)).parameters
        assert not {"confirm", "yes", "as_"} & set(params), name


def test_the_module_never_prints_prompts_or_checks_a_role() -> None:
    """The library imports nothing of the CLI: no console, no typer, no commands."""
    tree = ast.parse(SRC.read_text())
    imported = {
        alias.name if isinstance(node, ast.Import) else f"{node.module}.{alias.name}"
        for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not [m for m in imported if m.startswith(("typer", "rich", "vl.commands", "vl.lib.output"))]
    calls = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not calls & {"print", "input", "require_operator"}


# --------------------------------------------------------------------------- #
# the lock cases of §12, between real processes
# --------------------------------------------------------------------------- #

# Starts globo/node1 with the real start_node at start_at, and prints the outcome.
# The copy into the node's directory is slowed by half a second (see below).
# The stand-in node is a shell script, so /proc/<pid>/exe is /bin/sh, not the
# node's own binary; for this test only, "our process" is narrowed to "alive,
# with the node's binary on its command line".
_STARTER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from vl.lib import nodeops, proc, store

    def ours(pid, exe, **_):
        try:
            return proc.is_alive(pid) and str(exe) in Path(f"/proc/{pid}/cmdline").read_bytes().decode()
        except (OSError, TypeError):
            return False

    proc.is_our_process = ours
    # Slow the copy down, so that without the lock both would read the row as NEW
    # and spawn; with it, the second waits and finds the first running.
    real_populate = nodeops.populate_node_dir

    def slow_populate(*args, **kwargs):
        root = real_populate(*args, **kwargs)
        time.sleep(0.5)
        return root

    nodeops.populate_node_dir = slow_populate
    wait, start_at = float(sys.argv[1]), float(sys.argv[2])
    while time.time() < start_at:
        time.sleep(0.005)
    result = nodeops.start_node(store.get_environment("local"), "globo", 1, lambda text: None, lock_wait=wait)
    print(result.outcome.value, result.pid, result.detail, sep="|")
    """
)


@pytest.fixture
def real_install(isolated_home: Path) -> Iterator[Path]:
    """A shared install whose verilock is a stand-in, and a node created from it."""
    install = isolated_home / ".veritaslock"
    for sub, name, text in (
        ("bin", "verilock", "#!/bin/sh\nsleep 60\n"),  # never a real verilock
        ("lib", "liblog4cplus.so.3", "lib"),
        ("etc", "log4cplus.properties", "x"),
        ("etc", "verilock-kafka-consumer.conf", "x"),
    ):
        (install / sub).mkdir(parents=True, exist_ok=True)
        (install / sub / name).write_text(text)
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "t0")
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "client-1", "client-1", "globo-1")
    store.set_svc_acct(ident.id, "local", "globo", "client-1", "secret", role="NODE",
                       public_key_path=str(isolated_home / "public.key"),
                       private_key_path=str(isolated_home / "private.key"))
    store.add_node("local", "globo", 1, ident.id)
    root = nodeops.node_root("globo", 1)
    for sub in ("bin", "etc", "data", "identity", "lib", "snapshots"):
        (root / sub).mkdir(parents=True)
    yield root / "bin" / "verilock"
    for pid in _live_nodes(root / "bin" / "verilock"):
        kill_stand_in(pid)


def _live_nodes(binary: Path) -> list[int]:
    pids = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                cmdline = (entry / "cmdline").read_bytes().decode()
            except OSError:
                continue
            if str(binary) in cmdline and "python" not in cmdline:
                pids.append(int(entry.name))
    return pids


def _start_twice(isolated_home: Path, isolated_store: Path, waits: tuple[float, float]) -> list[str]:
    env = {**os.environ, "HOME": str(isolated_home), "VL_STORE_PATH": str(isolated_store)}
    start_at = time.time() + 1.5
    children = [
        subprocess.Popen([sys.executable, "-c", _STARTER, str(wait), str(start_at)],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for wait in waits
    ]
    outs = [child.communicate(timeout=60) for child in children]
    assert all(child.returncode == 0 for child in children), [err for _, err in outs]
    return sorted(out.strip().split("|")[0] for out, _ in outs)


def test_two_processes_starting_one_node_give_one_started_and_one_already_running(
    real_install: Path, isolated_home: Path, isolated_store: Path
) -> None:
    outcomes = _start_twice(isolated_home, isolated_store, (10, 10))

    assert outcomes == ["ALREADY_RUNNING", "STARTED"]
    live = _live_nodes(real_install)
    assert len(live) == 1
    assert store.get_node("local", "globo", 1).pid == live[0]


def test_two_processes_starting_one_node_with_a_short_wait_give_one_started_and_one_busy(
    real_install: Path, isolated_home: Path, isolated_store: Path
) -> None:
    outcomes = _start_twice(isolated_home, isolated_store, (0.2, 0.2))

    assert outcomes == ["FAILED", "STARTED"]  # the loser waited 0.2 s and found it busy
    assert len(_live_nodes(real_install)) == 1


# Holds globo/node1's lock as a start does; on "go" writes its own pid to the row
# and releases the lock, then stays alive until killed.
_STARTING = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path
    from vl.lib import nodelock, nodeops, store

    with nodelock.node_lock(nodeops.node_root("globo", 1)):
        print("locked", flush=True)
        sys.stdin.readline()
        store.set_pid(store.get_node("local", "globo", 1), os.getpid())
    print("written", flush=True)
    sys.stdin.readline()
    """
)


def test_the_repair_never_clobbers_a_start_in_progress(
    isolated_home: Path, isolated_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "t0")
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "client-1", "client-1", "globo-1")
    store.set_svc_acct(ident.id, "local", "globo", "client-1", "secret", role="NODE")
    stale = store.set_pid(store.add_node("local", "globo", 1, ident.id), 999_999)  # RUNNING, dead pid
    nodeops.node_root("globo", 1).mkdir(parents=True)
    env = {**os.environ, "HOME": str(isolated_home), "VL_STORE_PATH": str(isolated_store)}
    starter = subprocess.Popen([sys.executable, "-c", _STARTING], env=env, text=True,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert starter.stdin is not None and starter.stdout is not None
    try:
        assert starter.stdout.readline().strip() == "locked"

        began = time.monotonic()
        shown = nodeops.repair(stale)  # `vl node list` finds the dead pid mid-start

        assert time.monotonic() - began < 0.5  # it skipped itself, it did not wait
        assert shown == stale
        row = store.get_node("local", "globo", 1)
        assert (row.node_state, row.pid) == ("RUNNING", 999_999)  # nothing written

        starter.stdin.write("go\n")
        starter.stdin.flush()
        assert starter.stdout.readline().strip() == "written"
        # A repair that read the stale row before the start wrote, and gets the
        # lock after it: the row changed since, so it is left alone.
        assert nodeops.repair(stale).pid == starter.pid
        row = store.get_node("local", "globo", 1)
        assert (row.node_state, row.pid) == ("RUNNING", starter.pid)
    finally:
        starter.kill()
        starter.wait()
