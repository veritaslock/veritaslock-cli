"""Shared node operations (vl-agent-cli-spec.md §4): one code path for `vl` and the node agent.

Each operation does the work and returns a :class:`NodeResult`; the thin
commands in ``vl.commands`` check the operator's role, prompt where they prompt,
call these, and print. So that both callers get the same behaviour:

- **No function prompts** and **none checks the operator's role**.
- **Progress goes to ``emit(text)``**, never straight to the console: the CLI
  passes its ``note`` (the same lines it has always printed), the agent its
  logger.
- **start, stop and reset hold the node's lifecycle lock** (``vl.lib.nodelock``)
  for their whole duration, and re-read the node's row under it; the repair
  that `vl node list` and `show` make takes it with a try and skips itself.
- A failure the operation knows (an early exit, a busy node, a stop that did
  not take) is a ``FAILED`` result. Anything unexpected (a store or network
  error, a missing file) is raised, as before; a caller that must never raise
  (the agent) turns it into ``FAILED`` itself.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from vl.lib import auth, nodelock, proc, store

Emit = Callable[[str], None]


class Outcome(str, Enum):
    """``cp-agent-commands-spec.md`` §3.4's node outcomes, plus ``SKIPPED_UP``
    (reset found the node up), which the agent reports as ``FAILED``."""

    STARTED = "STARTED"
    ALREADY_RUNNING = "ALREADY_RUNNING"
    STOPPED = "STOPPED"
    ALREADY_STOPPED = "ALREADY_STOPPED"
    RESET = "RESET"
    SKIPPED_UP = "SKIPPED_UP"
    FAILED = "FAILED"


@dataclass(frozen=True)
class NodeResult:
    outcome: Outcome
    detail: str
    exit_code: int | None = None
    pid: int | None = None


STILL_UP_DETAIL = "still reported UP by the control plane"


# --------------------------------------------------------------------------- #
# Where a node lives
# --------------------------------------------------------------------------- #


def node_root(org: str, n: int) -> Path:
    return Path.home() / "orgs" / org / "nodes" / f"node{n}"


def shared_install() -> Path:
    """``$HOME/.veritaslock``: the binaries, libraries and config every node copies."""
    return Path.home() / ".veritaslock"


def log_path(org: str, n: int) -> Path:
    """Where a node's stdout and stderr go: ``$HOME/tmp/<org>-node<n>.out``."""
    return Path.home() / "tmp" / f"{org}-node{n}.out"


def node_executable(node: store.Node) -> Path:
    """The binary a process must be running to count as this node's own (§4c)."""
    return node_root(node.org_name, node.org_node) / "bin" / "verilock"


def is_our_process(node: store.Node) -> bool:
    return proc.is_our_process(node.pid, node_executable(node))


def _label(node: store.Node) -> str:
    return f"{node.org_name}/node{node.org_node}"


def _busy() -> NodeResult:
    return NodeResult(Outcome.FAILED, nodelock.BUSY_DETAIL)


# --------------------------------------------------------------------------- #
# start (vl-node-spec.md §5.2)
# --------------------------------------------------------------------------- #

# Hints for a node that exits right after it is started (vl-node-spec.md §5.2
# step 4), keyed by its exit code or a marker in its log tail. First match wins.
# Exit code 1 (any other startup failure) has no hint: the log tail says why.
_START_FAILURE_HINTS: tuple[tuple[str | int, str], ...] = (
    (
        69,  # EX_UNAVAILABLE: the control plane answered 409 NOT_DOWN
        "The control plane still has this node as UP. Wait about 30 seconds for its "
        "heartbeat timeout and start it again.",
    ),
    (
        "NOT_DOWN",  # older binaries abort (SIGABRT) instead, so match the log text
        "The control plane still has this node as UP. A kill -9 gives a node no chance "
        "to say it is going down, and the control plane marks it DOWN after about 30 "
        "seconds without a heartbeat. Wait about 30 seconds and start it again.",
    ),
    (
        75,  # EX_TEMPFAIL
        "The node asked to be started again later (for example the control plane still "
        "has it as UP, or its chain was out of sync and a restart will replay). Check "
        "the log, wait about 30 seconds and start it again.",
    ),
    (
        74,  # EX_IOERR
        "The node stopped because of a storage failure. Do not restart it "
        "automatically: check the disk first.",
    ),
)


def describe_exit(returncode: int) -> str:
    """How a process ended: ``killed by SIGABRT`` or ``exited with code 3``."""
    if returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:
            name = f"signal {-returncode}"
        return f"killed by {name}"
    return f"exited with code {returncode}"


def start_failure_reason(returncode: int) -> str:
    return f"exited right after starting ({describe_exit(returncode)})"


def start_failure_hint(returncode: int, log_tail: list[str]) -> str | None:
    text = "\n".join(log_tail)
    for key, hint in _START_FAILURE_HINTS:
        matched = key in text if isinstance(key, str) else key == returncode
        if matched:
            return hint
    return None


def read_tail(path: Path, n: int = 50) -> list[str] | None:
    """The last ``n`` lines of ``path``; ``None`` if it doesn't exist."""
    try:
        with open(path, "r", errors="replace") as f:
            return [line.rstrip() for line in f.readlines()[-n:]]
    except FileNotFoundError:
        return None


def create_sym_link(private_key_link: Path, private_key_path: str) -> None:
    # Remove old link if it exists
    if private_key_link.exists() or private_key_link.is_symlink():
        private_key_link.unlink()
    private_key_link.symlink_to(private_key_path)


def create_lib_symlink(node_lib_dir: Path) -> None:
    target = node_lib_dir / "liblog4cplus.so.3"
    link = node_lib_dir / "liblog4cplus.so"
    create_sym_link(link, target.name)


def generate_veritaslock_config(environment: store.Environment, dest_config_dir: Path, node: store.Node) -> None:
    veritaslock_config_file = dest_config_dir / "veritas-lock.conf"
    assert all(v is not None for v in [environment.cp_base_url,
                                       environment.idp_base_url,
                                       environment.token_url,
                                       environment.schema_reg_url,
                                       environment.kafka_bootstrap])

    with open(veritaslock_config_file, "w") as veritaslock_config:
        veritaslock_config.write(f"kafka.broker={environment.kafka_bootstrap}\n")
        veritaslock_config.write(f"schema.registry.url={environment.schema_reg_url}\n")
        veritaslock_config.write("veritas.data.dir=data\n")
        veritaslock_config.write(f"token.url={environment.token_url}\n")
        veritaslock_config.write(f"control.plane.base.url={environment.cp_base_url}\n")
        veritaslock_config.write(f"identity.provider.base.url={environment.idp_base_url}\n")
        veritaslock_config.write(f"http.port={node.port}\n")
        veritaslock_config.write(f"org.node={node.org_node}")


def populate_bin_dir(node_root: Path, src_dir: Path) -> None:
    src_bin = src_dir / "bin"
    dst_bin = node_root / "bin"
    for src_file in src_bin.iterdir():
        if src_file.is_file():
            dst_file = dst_bin / src_file.name
            shutil.copy2(src_file, dst_file)
            # chmod +x (preserve existing bits, add execute)
            st = dst_file.stat()
            os.chmod(dst_file, st.st_mode | 0o111)


def populate_lib_dir(node_root: Path, src_dir: Path) -> None:
    src_lib_dir = src_dir / "lib"
    dest_lib_dir = node_root / "lib"
    shutil.copy2(src_lib_dir / "liblog4cplus.so.3", dest_lib_dir)
    create_lib_symlink(dest_lib_dir)


def populate_config_dir(environment: store.Environment, node_root: Path, src_dir: Path, node: store.Node) -> None:
    src_config_dir = src_dir / "etc"
    dest_config_dir = node_root / "etc"
    shutil.copy2(src_config_dir / "log4cplus.properties", dest_config_dir)
    shutil.copy2(src_config_dir / "verilock-kafka-consumer.conf", dest_config_dir)
    generate_veritaslock_config(environment, dest_config_dir, node)


def populate_identity_dir(node: store.Node, node_root: Path) -> None:
    svc_acct_id = node.svc_acct_id
    svc_acct_ = store.get_svc_acct(svc_acct_id)
    if svc_acct_ is not None:
        private_key_path = svc_acct_.private_key_path
        public_key_path = svc_acct_.public_key_path
        assert private_key_path is not None
        assert public_key_path is not None

        identity_dir = node_root / "identity"
        client_id = svc_acct_.client_id
        client_id_file = identity_dir / "client_id"
        with client_id_file.open("w") as f:
            f.write(client_id)

        private_key_link = identity_dir / "private.key"
        public_key_link = identity_dir / "public.key"

        create_sym_link(private_key_link, private_key_path)
        create_sym_link(public_key_link, public_key_path)


def populate_node_dir(environment: store.Environment, org: str, n: int, node: store.Node) -> Path:
    """Copy the shared install into the node's own directory and render its config (§5.2 step 2)."""
    root = node_root(org, n)
    src_dir = shared_install()
    populate_bin_dir(root, src_dir)
    populate_config_dir(environment, root, src_dir, node)
    populate_lib_dir(root, src_dir)
    populate_identity_dir(node, root)
    return root


def spawn_node(node: store.Node, root: Path, emit: Emit) -> NodeResult:
    """Spawn the node detached, record its pid, and check it is still alive a second later.

    Its own session (``start_new_session=True``), nothing tied to the caller, so it
    outlives a `vl` command or an agent that exits or crashes (vl-agent-spec.md §8
    item 6). Output goes to :func:`log_path`.
    """
    org, n = node.org_name, node.org_node
    log = log_path(org, n)
    log.parent.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["VERITAS_INSTALL_ROOT"] = str(root)
    env["LD_PRELOAD"] = "/usr/lib/x86_64-linux-gnu/libjemalloc.so.2"
    env["MALLOC_CONF"] = (
        "background_thread:true,"
        "dirty_decay_ms:1000,"
        "muzzy_decay_ms:0,"
        "narenas:2"
    )
    with open(log, "w") as log_file:
        child = subprocess.Popen(
            [str(root / "bin" / "verilock")],
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )

    node = store.set_pid(node, child.pid)
    # Long enough to catch a rejected registration at the control plane, not just a crash.
    time.sleep(1)
    returncode = child.poll()
    if returncode is not None:
        store.set_node_stopped(node)
        detail = start_failure_reason(returncode)
        hint = start_failure_hint(returncode, read_tail(log) or [])
        if hint:
            detail = f"{detail}. {hint}"
        return NodeResult(Outcome.FAILED, detail, exit_code=returncode)
    emit(f"Started {org}/node{n} (pid: {child.pid}).")
    return NodeResult(Outcome.STARTED, f"pid {child.pid}", pid=child.pid)


def start_node(
    environment: store.Environment,
    org_name: str,
    org_node: int,
    emit: Emit,
    *,
    lock_wait: float = nodelock.DEFAULT_WAIT_SECONDS,
) -> NodeResult:
    """``vl node start`` §5.2: ``STARTED`` (pid), ``ALREADY_RUNNING``, or ``FAILED``
    (an early exit, with its exit code and the hint; or the node is busy)."""
    try:
        with nodelock.node_lock(node_root(org_name, org_node), wait=lock_wait):
            node = store.get_node(environment.name, org_name, org_node)
            if node.node_state == "RUNNING" and is_our_process(node):
                emit(f"{org_name} node{org_node} is already running (pid {node.pid})")
                return NodeResult(Outcome.ALREADY_RUNNING, f"already running (pid {node.pid})", pid=node.pid)
            root = populate_node_dir(environment, org_name, org_node, node)
            return spawn_node(node, root, emit)
    except nodelock.NodeBusyError:
        return _busy()


# --------------------------------------------------------------------------- #
# stop (vl-node-spec.md §5.3)
# --------------------------------------------------------------------------- #


def shutdown_node(node: store.Node, emit: Emit) -> bool:
    """SIGTERM, poll, then SIGKILL. Only call this for a pid that passed :func:`is_our_process`.

    Polls with the same §4c check, so a zombie or a pid that's been reused counts
    as gone and is never signalled again.
    """
    assert node.pid is not None
    pid = node.pid
    label = _label(node)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        emit(f"{pid} was not running or was already terminated")
        return True

    deadline = time.time() + 10.0
    while time.time() < deadline:
        if not is_our_process(node):
            emit(f"Stopped {label} (pid: {pid}).")
            return True
        time.sleep(0.25)

    # Still alive after the timeout: SIGKILL, then a brief pause for the kernel to reap.
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        emit(f"Terminated {label} (pid: {pid}).")
        return True
    except PermissionError:
        pass
    time.sleep(0.1)

    if not is_our_process(node):
        emit(f"Terminated {label} (pid: {pid}).")
        return True

    emit(f"Failure to shutdown {label} (pid: {pid}).")
    return False


def stop_node(
    environment: store.Environment,
    org_name: str,
    org_node: int,
    emit: Emit,
    *,
    lock_wait: float = nodelock.DEFAULT_WAIT_SECONDS,
) -> NodeResult:
    """``vl node stop`` §5.3: ``STOPPED``, ``ALREADY_STOPPED`` (no pid, or a stale
    one, which is cleared), or ``FAILED`` (it would not die; or the node is busy)."""
    try:
        with nodelock.node_lock(node_root(org_name, org_node), wait=lock_wait):
            node = store.get_node(environment.name, org_name, org_node)
            if node.pid is None:
                emit(f"{org_name}/node{org_node} is not running.")
                return NodeResult(Outcome.ALREADY_STOPPED, "not running")
            if not is_our_process(node):
                store.set_node_stopped(node)
                emit("done")
                return NodeResult(Outcome.ALREADY_STOPPED, f"not running (stale pid {node.pid} cleared)")
            pid = node.pid
            if shutdown_node(node, emit):
                store.set_node_stopped(node)
                return NodeResult(Outcome.STOPPED, f"pid {pid}", pid=pid)
            return NodeResult(Outcome.FAILED, f"still running after SIGTERM and SIGKILL (pid {pid})", pid=pid)
    except nodelock.NodeBusyError:
        return _busy()


def fetch_node_by_id(environment: store.Environment, node: store.Node) -> dict[str, Any] | None:
    """The control plane's view of the node, ``GET /v1/nodes?orgNode=``, as the node's own account."""
    acct = store.get_svc_acct(node.svc_acct_id)
    assert acct is not None
    assert acct.client_id is not None
    identity = store.get_identity_by_server_id(environment.name, acct.client_id, "SERVICE_ACCOUNT")
    assert identity is not None
    result: dict[str, Any] = auth.authed_call(
        identity,
        environment.idp_base_url,
        lambda c: c.get(
            "/v1/nodes", params={"orgNode": node.org_node}
        ),
        environment.cp_base_url
    )
    return result


def node_is_up(environment: store.Environment, node: store.Node) -> bool:
    """One control plane check: the node's state is anything but ``DOWN``."""
    node_dto = fetch_node_by_id(environment, node)
    assert node_dto is not None
    state: str = node_dto["state"]
    return state != "DOWN"


def stop_nodes_to_down(
    environment: store.Environment,
    nodes: list[store.Node],
    emit: Emit,
    *,
    polls: int = 20,
    poll_interval: float = 0.25,
    lock_wait: float = nodelock.DEFAULT_WAIT_SECONDS,
) -> list[tuple[store.Node, NodeResult]]:
    """Stop each node (descending), then wait until the control plane reports it ``DOWN``.

    The org routine of ``vl org stop`` and the agent's ``STOP``. Per node:
    :func:`stop_node`, then up to ``polls`` checks of the control plane as the
    node's own account; still up after them is ``FAILED`` "still reported UP by
    the control plane". The pauses between nodes let the network consume the
    protocol events: 0.5 seconds, 2 seconds before GLOBO's last node. Any
    exception is that node's ``FAILED`` and the loop goes on. Returns
    ``(node, result)`` in the order stopped.
    """
    ordered = list(reversed(nodes))
    results: list[tuple[store.Node, NodeResult]] = []
    for idx, node_ in enumerate(ordered):
        try:
            is_last = idx == len(ordered) - 1
            if is_last and node_.org_name == "globo":
                time.sleep(2)
            elif idx != 0:
                time.sleep(0.5)

            result = stop_node(environment, node_.org_name, node_.org_node, emit, lock_wait=lock_wait)
            if result.detail == nodelock.BUSY_DETAIL:
                emit(f"Warning: {_label(node_)}: {nodelock.BUSY_DETAIL}")
                results.append((node_, result))
                continue
            down = False
            for _ in range(polls):
                time.sleep(poll_interval)
                if not node_is_up(environment, node_):
                    down = True
                    break
            if not down:
                emit(f"Warning: {node_.org_name}-node{node_.org_node} is still reported UP by the control plane.")
                result = NodeResult(Outcome.FAILED, STILL_UP_DETAIL, pid=result.pid)
            results.append((node_, result))
        except Exception as e:
            emit(f"Warning: {e}")
            results.append((node_, NodeResult(Outcome.FAILED, str(e))))
    return results


# --------------------------------------------------------------------------- #
# reset (vl-node-spec.md §5.4)
# --------------------------------------------------------------------------- #


def clear_node_data(root: Path, include_snapshots: bool) -> None:
    """Empty ``data/`` (and ``snapshots/``): the node's local blockchain state."""
    data_dir = root / "data"
    shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    if include_snapshots:
        snapshots_dir = root / "snapshots"
        shutil.rmtree(snapshots_dir, ignore_errors=True)
        snapshots_dir.mkdir(parents=True, exist_ok=True)


def reset_node(
    environment: store.Environment,
    org_name: str,
    org_node: int,
    emit: Emit,
    *,
    include_snapshots: bool = False,
    lock_wait: float = nodelock.DEFAULT_WAIT_SECONDS,
) -> NodeResult:
    """``vl node reset`` §5.4: ``RESET``, ``SKIPPED_UP`` (the control plane reports
    it up: one check, as the node's own account, unless it is NEW), or ``FAILED``
    (the node is busy)."""
    try:
        with nodelock.node_lock(node_root(org_name, org_node), wait=lock_wait):
            node = store.get_node(environment.name, org_name, org_node)
            label = _label(node)
            if node.node_state != "NEW" and node_is_up(environment, node):
                emit(f"{label} is still reported UP by the control plane — stop it first or try again.")
                return NodeResult(Outcome.SKIPPED_UP, STILL_UP_DETAIL)
            clear_node_data(node_root(org_name, org_node), include_snapshots)
            what = "data and snapshots directories" if include_snapshots else "data directory"
            emit(f"Deleted {label}'s {what}.")
            return NodeResult(Outcome.RESET, f"deleted its {what}")
    except nodelock.NodeBusyError:
        return _busy()


# --------------------------------------------------------------------------- #
# repair (vl-node-spec.md §4c)
# --------------------------------------------------------------------------- #


def repair(node: store.Node) -> store.Node:
    """A ``RUNNING`` row whose pid isn't this node's own live process becomes
    ``STOPPED`` with no pid. Persisted, local, no control-plane call.

    Takes the node's lock with a try: if a start, stop or reset holds it, the
    repair skips itself and the row is shown as it is. Under the lock, the row
    is stopped only if it still has the pid it was read with.
    """
    if node.node_state == "RUNNING" and not is_our_process(node):
        with nodelock.try_node_lock(node_root(node.org_name, node.org_node)) as held:
            if not held:
                return node
            return store.set_node_stopped_if_unchanged(node)
    return node
