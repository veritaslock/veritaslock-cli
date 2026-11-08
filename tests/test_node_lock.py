"""The per-node lifecycle lock (vl-agent-cli-spec.md §5.1, §12).

Contention is between real processes (flock is per open file description, so
threads in one process would prove little). Everything runs under the test's
temporary HOME; the only spawned "node" is a stand-in shell script.
"""

from __future__ import annotations

import inspect
import os
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from _helpers import kill_stand_in
from vl.lib import nodelock, nodeops, store

# Holds the node's lock for argv[2] seconds, saying "locked" once it has it.
_HOLDER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from vl.lib import nodelock

    with nodelock.node_lock(Path(sys.argv[1])):
        print("locked", flush=True)
        time.sleep(float(sys.argv[2]))
    """
)

# Tries the lock once, without waiting, and prints whether it got it.
_TRIER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from vl.lib import nodelock

    with nodelock.try_node_lock(Path(sys.argv[1])) as held:
        print("held" if held else "busy", flush=True)
    """
)


@pytest.fixture
def node_root(isolated_home: Path) -> Path:
    root = isolated_home / "orgs" / "globo" / "nodes" / "node1"
    (root / "data").mkdir(parents=True)
    return root


@pytest.fixture
def holder(node_root: Path) -> Iterator[Callable[[float], subprocess.Popen[str]]]:
    """Start a process holding the lock for ``seconds``; returns once it has it."""
    started: list[subprocess.Popen[str]] = []

    def hold(seconds: float) -> subprocess.Popen[str]:
        child = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(node_root), str(seconds)],
            stdout=subprocess.PIPE, text=True,
        )
        started.append(child)
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        return child

    yield hold
    for child in started:
        child.kill()
        child.wait()


def _try_from_another_process(node_root: Path) -> str:
    out = subprocess.run(
        [sys.executable, "-c", _TRIER, str(node_root)], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_the_default_wait_is_10_seconds() -> None:
    assert nodelock.DEFAULT_WAIT_SECONDS == 10
    assert inspect.signature(nodelock.node_lock).parameters["wait"].default == 10


def test_the_lock_file_is_beside_data_not_inside_it(node_root: Path) -> None:
    with nodelock.node_lock(node_root):
        lock = nodelock.lock_path(node_root)
        assert lock == node_root / ".lifecycle.lock" and lock.exists()
        assert (node_root / "data") not in lock.parents
        nodeops.clear_node_data(node_root, include_snapshots=True)  # what reset deletes
        assert lock.exists()
        assert _try_from_another_process(node_root) == "busy"  # still the same lock


def test_a_second_process_waits_for_the_lock_then_gets_it(node_root: Path, holder: Callable[[float], subprocess.Popen[str]]) -> None:
    holder(1.0)
    began = time.monotonic()

    with nodelock.node_lock(node_root, wait=5):
        waited = time.monotonic() - began

    assert 0.5 < waited < 4


def test_the_wait_runs_out_with_another_operation_is_in_progress(node_root: Path, holder: Callable[[float], subprocess.Popen[str]]) -> None:
    holder(30)
    began = time.monotonic()

    with pytest.raises(nodelock.NodeBusyError, match="^another operation is in progress$"):
        with nodelock.node_lock(node_root, wait=0.5):
            pytest.fail("got a lock another process holds")

    assert 0.5 <= time.monotonic() - began < 3


def test_the_try_lock_skips_at_once_when_busy_and_holds_when_free(node_root: Path, holder: Callable[[float], subprocess.Popen[str]]) -> None:
    child = holder(30)
    began = time.monotonic()
    with nodelock.try_node_lock(node_root) as held:
        assert held is False
    assert time.monotonic() - began < 0.5

    child.kill()
    child.wait()
    with nodelock.try_node_lock(node_root) as held:
        assert held is True
        assert _try_from_another_process(node_root) == "busy"
    assert _try_from_another_process(node_root) == "held"


def test_without_a_node_directory_both_locks_proceed_and_create_nothing(isolated_home: Path) -> None:
    missing = isolated_home / "orgs" / "globo" / "nodes" / "node9"
    ran = False
    with nodelock.node_lock(missing, wait=0):
        ran = True
    assert ran
    with nodelock.try_node_lock(missing) as held:
        assert held is True
    assert not missing.exists()


def test_a_node_spawned_under_the_lock_does_not_keep_it(node_root: Path) -> None:
    """The node outlives the operation, the lock does not: the spawn (the real
    spawn_node) does not hand the lock's descriptor to the node."""
    (node_root / "bin").mkdir()
    binary = node_root / "bin" / "verilock"
    binary.write_text("#!/bin/sh\nsleep 60\n")  # a stand-in node: never a real verilock
    binary.chmod(0o755)
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "t0")
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "globo-1", "globo-1")
    store.set_svc_acct(ident.id, "local", "globo", "client-1", "secret", role="NODE")
    row = store.add_node("local", "globo", 1, ident.id)

    with nodelock.node_lock(node_root):
        nodeops.spawn_node(row, node_root, lambda text: None)
    pid = store.get_node("local", "globo", 1).pid
    assert pid is not None
    try:
        assert Path(f"/proc/{pid}/cmdline").read_bytes().decode().count(str(binary)) == 1
        assert _try_from_another_process(node_root) == "held"
    finally:
        kill_stand_in(pid)


# Takes the lock, spawns the node with the real spawn_node, then dies
# without releasing the lock (a vl or an agent killed mid-operation).
_CRASHER = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path
    from vl.lib import nodelock, nodeops, store

    root = Path(sys.argv[1])
    row = store.get_node("local", "globo", 1)
    with nodelock.node_lock(root):
        nodeops.spawn_node(row, root, print)
        print(store.get_node("local", "globo", 1).pid, flush=True)
        os._exit(0)
    """
)


def test_a_node_does_not_keep_the_lock_when_its_starter_dies_holding_it(
    node_root: Path, isolated_home: Path, isolated_store: Path
) -> None:
    """flock belongs to the open file description, so an inherited descriptor
    would keep the lock alive in the node after the starter died; it must not."""
    (node_root / "bin").mkdir()
    binary = node_root / "bin" / "verilock"
    binary.write_text("#!/bin/sh\nsleep 60\n")  # a stand-in node: never a real verilock
    binary.chmod(0o755)
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "t0")
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "globo-1", "globo-1")
    store.set_svc_acct(ident.id, "local", "globo", "client-1", "secret", role="NODE")
    store.add_node("local", "globo", 1, ident.id)
    env = {**os.environ, "HOME": str(isolated_home), "VL_STORE_PATH": str(isolated_store)}

    crashed = subprocess.run(
        [sys.executable, "-c", _CRASHER, str(node_root)], env=env, capture_output=True, text=True, timeout=30
    )
    assert crashed.returncode == 0, crashed.stderr
    pid = int(crashed.stdout.split()[-1])
    try:
        assert Path(f"/proc/{pid}/cmdline").read_bytes().decode().count(str(binary)) == 1
        assert _try_from_another_process(node_root) == "held"
    finally:
        kill_stand_in(pid)


def test_after_start_node_returns_another_process_can_take_the_lock_at_once(
    isolated_home: Path,
) -> None:
    """The real start_node, from the shared install to the spawn: once it returns
    STARTED with the node still alive, the node's lock is free (the node did
    not inherit its descriptor)."""
    install = isolated_home / ".veritaslock"
    for sub, name, text in (
        ("bin", "verilock", "#!/bin/sh\nsleep 60\n"),  # a stand-in node: never a real verilock
        ("lib", "liblog4cplus.so.3", "lib"),
        ("etc", "log4cplus.properties", "x"),
        ("etc", "verilock-kafka-consumer.conf", "x"),
    ):
        (install / sub).mkdir(parents=True, exist_ok=True)
        (install / sub / name).write_text(text)
    root = isolated_home / "orgs" / "globo" / "nodes" / "node1"
    for sub in ("bin", "etc", "data", "identity", "lib", "snapshots"):
        (root / sub).mkdir(parents=True)
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "t0")
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "client-1", "client-1", "globo-1")
    store.set_svc_acct(ident.id, "local", "globo", "client-1", "secret", role="NODE",
                       public_key_path=str(isolated_home / "public.key"),
                       private_key_path=str(isolated_home / "private.key"))
    store.add_node("local", "globo", 1, ident.id)
    lines: list[str] = []

    result = nodeops.start_node(store.get_environment("local"), "globo", 1, lines.append)

    assert result.outcome is nodeops.Outcome.STARTED, result
    assert result.pid is not None
    try:
        assert lines == [f"Started globo/node1 (pid: {result.pid})."]
        assert Path(f"/proc/{result.pid}/cmdline").read_bytes().decode().count(str(root / "bin" / "verilock")) == 1
        began = time.monotonic()
        assert _try_from_another_process(root) == "held"
        assert time.monotonic() - began < 5
    finally:
        kill_stand_in(result.pid)
