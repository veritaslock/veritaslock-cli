"""How `vl node start` spawns a node (vl-node-spec.md §5.2, vl-agent-spec.md §8 item 6).

A node must outlive whatever started it (a `vl` command, or an agent that exits or
crashes): its own session, no parent-death signal, nothing that kills it with the
caller. Every test runs under a temporary HOME and store; the one real process is a
stand-in binary under that HOME, killed at the end.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from vl.commands import node as node_cmd
from vl.lib import proc, store


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


def _node_row() -> store.Node:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "2026-01-01T00:00:00Z")
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-globo-1", "globo-1", "globo-1")
    store.set_svc_acct(ident.id, "local", "globo", "client-globo-1", "secret", role="NODE")
    return store.add_node("local", "globo", 1, ident.id, 7001)


class _Running:
    """A Popen stand-in for a process that is still running."""

    pid = 4242

    def poll(self) -> None:
        return None


def test_start_spawns_the_node_in_its_own_session_with_nothing_tied_to_the_caller(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def popen(*args: Any, **kwargs: Any) -> _Running:
        calls.append((args, kwargs))
        return _Running()

    monkeypatch.setattr(node_cmd.subprocess, "Popen", popen)
    monkeypatch.setattr(node_cmd.time, "sleep", lambda s: None)
    node_root = home / "orgs" / "globo" / "nodes" / "node1"

    node_cmd.start_verilock(_node_row(), node_root, "globo", 1)

    [(args, kwargs)] = calls
    assert args == ([str(node_root / "bin" / "verilock")],)
    assert kwargs["start_new_session"] is True
    # Nothing that ties the child's life, or its group, to the caller.
    for tie in ("preexec_fn", "process_group", "pass_fds"):
        assert kwargs.get(tie) is None, tie
    assert kwargs.get("close_fds", True) is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    log = home / "tmp" / "globo-node1.out"
    assert kwargs["stdout"].name == str(log) and kwargs["stderr"].name == str(log)
    assert kwargs["env"]["VERITAS_INSTALL_ROOT"] == str(node_root)
    assert store.get_node("local", "globo", 1).pid == 4242


# Run in a separate Python process that then exits: the node it started must survive it.
_STARTER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from vl.commands import node as node_cmd
    from vl.lib import store

    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "2026-01-01T00:00:00Z")
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-globo-1", "globo-1", "globo-1")
    store.set_svc_acct(ident.id, "local", "globo", "client-globo-1", "secret", role="NODE")
    row = store.add_node("local", "globo", 1, ident.id, 7001)
    node_cmd.start_verilock(row, Path(sys.argv[1]), "globo", 1)
    print(store.get_node("local", "globo", 1).pid)
    """
)


def test_a_node_survives_the_process_that_started_it(home: Path, isolated_store: Path) -> None:
    node_root = home / "orgs" / "globo" / "nodes" / "node1"
    (node_root / "bin").mkdir(parents=True)
    binary = node_root / "bin" / "verilock"
    binary.write_text("#!/bin/sh\nsleep 60\n")  # a stand-in node: never a real verilock
    binary.chmod(0o755)
    env = {**os.environ, "HOME": str(home), "VL_STORE_PATH": str(isolated_store)}
    env.pop("LD_PRELOAD", None)

    starter = subprocess.run(
        [sys.executable, "-c", _STARTER, str(node_root)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert starter.returncode == 0, starter.stderr
    pid = int(starter.stdout.split()[-1])
    try:
        time.sleep(0.2)
        # The starter is gone; the node is not. (The stand-in runs as /bin/sh, so its
        # command line names it, not its executable.)
        assert proc.is_alive(pid)
        assert str(binary) in Path(f"/proc/{pid}/cmdline").read_bytes().decode()
        assert os.getsid(pid) == pid  # the leader of its own session
        assert os.getsid(pid) != os.getsid(0)
        assert os.getpgid(pid) == pid
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
