"""`vl node` — manage VeritasLock nodes.

Authenticates with Phase 3's `--as` / `VL_IDENTITY` / default identity + cached
token flow throughout. See vl-node-spec.md.
"""

from __future__ import annotations

import secrets
import signal
from typing import Annotated, Any

import typer
import os
import time
import subprocess
import shutil

from vl.commands import svc_acct
from vl.commands._shared import (
    AsOption,
    EnvOption,
    report_errors,
    resolve_org, assert_label_free,
)
from vl.lib import store, roles
from vl.lib.cli import HelpOnErrorGroup
from vl.lib.output import render, note
from pathlib import Path

app = typer.Typer(
    help="Manage VeritasLock nodes.",
    no_args_is_help=True,
    cls=HelpOnErrorGroup
)


def _node_row(node: store.Node) -> dict[str, Any]:
    return {
        "org": node.org_name,
        "org_node": node.org_node,
        "port": node.port,
        "state": node.node_state,
        "created_at": node.created_at,
    }


def _resolve_org_node(env: str, org_name: str, org_node: int | None = None) -> int:
    if org_node is None:
        org_node = store.next_org_node(env, org_name)
    return org_node


def _resolve_port(port: int | None = None) -> int:
    if port is None:
        port = store.next_port()
    return port


def create_node_dir(org: str, n: int) -> Path:
    node_root = Path.home() / "orgs" / org / "nodes" / f"node{n}"
    node_bin_dir = node_root / "bin"
    node_etc_dir = node_root / "etc"
    node_data_dir = node_root / "data"
    node_identity_dir = node_root / "identity"
    node_lib_dir = node_root / "lib"
    node_snapshots_dir = node_root / "snapshots"
    if node_root.exists():
        note(f"WARNING: Directory already exists: {node_root}")
    else:
        node_root.mkdir(parents=True)
        node_bin_dir.mkdir()
        node_etc_dir.mkdir()
        node_data_dir.mkdir()
        node_identity_dir.mkdir()
        node_lib_dir.mkdir()
        node_snapshots_dir.mkdir()
        note(f"Created: {node_root}")
    return node_root


@app.command("create")
def create(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    org_node: Annotated[
        int | None,
        typer.Option("--node", help="Node number for org (defaults next available node number)."),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option("--port", help="Node port (default: next available port)."),
    ] = None,

    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Create a node """
    with report_errors():
        environment = store.get_environment(env)

        resolved_port = _resolve_port(port)
        caller = store.resolve_identity(environment.name, as_)
        org_dto = resolve_org(environment, org)
        org_name = str(org_dto["name"])
        org_node = _resolve_org_node(environment.name, org_name, org_node)
        identity_label = f"{org_name}-{org_node}"
        display_name = f"{org_name} node{org_node}"
        role = roles.ServiceAccountRole.NODE
        client_secret = secrets.token_hex(16)
        assert_label_free(environment.name, identity_label)
        _, identity_id = svc_acct.create_svc_acct(display_name, f"svc-acct for {display_name}", client_secret,
                                 environment, identity_label, org_name, caller, role)
        create_node_dir(org_name, org_node)
        _node = store.add_node(environment.name, org_name, org_node, identity_id, resolved_port)
    render(_node_row(_node), title="Node created")

class ProcessStillRunningError(Exception):
    pass

def check_node_state(node: store.Node) -> None:
    if node.node_state == "RUNNING" and node.pid is not None:
        try:
            os.kill(node.pid, 0)
            raise ProcessStillRunningError(f"{node.org_name} node{node.org_node} is already running (pid {node.pid})")
        except ProcessLookupError:
            note("Process does not exist.")
        except PermissionError:
            note("Process exists but cannot be signaled.")


def create_sym_link(private_key_link: Path, private_key_path: str) -> None:
    # Remove old link if it exists
    if private_key_link.exists() or private_key_link.is_symlink():
        private_key_link.unlink()
    private_key_link.symlink_to(private_key_path)


def create_lib_symlink(node_lib_dir: Path) -> None:
    target = node_lib_dir / "liblog4cplus.so.3"
    link = node_lib_dir / "liblog4cplus.so"
    create_sym_link(link, target.name)


def generate_veritaslock_config (environment: store.Environment, dest_config_dir: Path, node: store.Node) -> None:
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
    svc_acct = store.get_svc_acct(svc_acct_id)
    if svc_acct is not None:
        private_key_path = svc_acct.private_key_path
        public_key_path = svc_acct.public_key_path
        assert private_key_path is not None
        assert public_key_path is not None

        identity_dir = node_root / "identity"
        client_id = svc_acct.client_id
        client_id_file = identity_dir / "client_id"
        with client_id_file.open("w") as f:
            f.write(client_id)


        private_key_link = identity_dir / "private.key"
        public_key_link = identity_dir / "public.key"

        create_sym_link(private_key_link, private_key_path)
        create_sym_link(public_key_link, public_key_path)


def populate_node_dir(environment: store.Environment, org: str, n: int, node: store.Node) -> Path:
    node_root = Path.home() / "orgs" / org / "nodes" / f"node{n}"
    src_dir = Path.home() / ".veritaslock"
    populate_bin_dir(node_root, src_dir)
    populate_config_dir(environment, node_root, src_dir, node)
    populate_lib_dir(node_root, src_dir)
    populate_identity_dir(node, node_root)
    return node_root


def print_tail(path: Path, n: int = 50) -> None:
    try:
        with open(path, "r") as f:
            lines = f.readlines()
            tail = lines[-n:]
            for line in tail:
                note(line.rstrip())
    except FileNotFoundError:
        note(f"(log file {path} not found)")


def check_liveness(node: store.Node, proc: subprocess.Popen[bytes], log_path: Path) -> None:
    # Wait 1 second to ensure that the node starts correctly
    time.sleep(1)

    # If poll() returns something other than None, the process is dead
    if proc.poll() is not None:
        store.set_node_stopped(node)
        note(f"Process exited early with code {proc.returncode}")
        note("---- Log tail ----")
        print_tail(log_path, n=50)


def start_verilock(node: store.Node, node_root: Path, org: str, org_node: int) -> None:
    bin_path = node_root / "bin" / "verilock"
    log_dir = Path.home() / "tmp"
    Path.mkdir(log_dir, exist_ok=True)
    log_path = log_dir / f"{org}-node{org_node}.out"
    # Build environment
    env = os.environ.copy()
    env["VERITAS_INSTALL_ROOT"] = str(node_root)
    env["LD_PRELOAD"] = "/usr/lib/x86_64-linux-gnu/libjemalloc.so.2"
    env["MALLOC_CONF"] = (
        "background_thread:true,"
        "dirty_decay_ms:1000,"
        "muzzy_decay_ms:0,"
        "narenas:2"
    )
    # Open log file for stdout+stderr redirection
    # Spawn detached process
    with open(log_path, "w") as log_file:
        proc = subprocess.Popen(
            [str(bin_path)],
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )

    store.set_pid(node, proc.pid)
    check_liveness(node, proc, log_path)


@app.command("start")
def start(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    org_node: Annotated[int, typer.Argument(help="Node number to start")],
    env: EnvOption = None
) -> None:
    """Start the node """
    with report_errors():
        environment = store.get_environment(env)
        org_name = store.get_organization(environment.name, org).name
        node = store.get_node(environment.name, org_name, org_node)
        check_node_state(node)
        node_root = populate_node_dir(environment, org_name, org_node, node)
        start_verilock(node, node_root, org_name, org_node)


def is_running(node: store.Node) -> bool:
    assert node.pid is not None
    try:
        os.kill(node.pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def shutdown_node(node: store.Node) -> bool:
    assert node.pid is not None
    pid = node.pid
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        note(f"{pid} was not running or was already terminated")
        return True

    # Step 2: Poll using kill -0
    deadline = time.time() + 10.0
    while time.time() < deadline:
        try:
            os.kill(pid, 0)  # kill -0 probe
        except ProcessLookupError:
            note(f"{pid} was shutdown.")
            return True
        except PermissionError:
            # Process exists but cannot be signaled; treat as alive
            pass

        time.sleep(0.25)

    # Step 3: Still alive → SIGKILL
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        note(f"{pid} was terminated")
        return True
    except PermissionError:
        pass
    # Step 4: Brief pause to allow kernel to reap
    time.sleep(0.1)

    # Final check
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        note(f"{pid} was terminated")
        return True
    except PermissionError:
        pass

    note(f"Failure to shutdown node {node.org_node} with pid:{pid}.")
    return False


@app.command("stop")
def stop(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    org_node: Annotated[int, typer.Argument(help="Node number to stop")],
    env: EnvOption = None
) -> None:
    """Stop the node """
    with report_errors():
        environment = store.get_environment(env)
        org_name = store.get_organization(environment.name, org).name
        node = store.get_node(environment.name, org_name, org_node)
        if node.pid is None:
            note("not running (or not started by vl).")
        elif is_running(node):
            if shutdown_node(node):
                store.set_node_stopped(node)
        else:
            store.set_node_stopped(node)
            note("done")
