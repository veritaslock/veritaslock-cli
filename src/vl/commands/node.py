"""`vl node` — manage VeritasLock nodes.

Authenticates with Phase 3's `--as` / `VL_IDENTITY` / default identity + cached
token flow throughout. See vl-node-spec.md.
"""

from __future__ import annotations

import secrets
import signal
import sys
from os import mkdir
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
    resolve_org, assert_label_free, CliError,
)
from vl.lib import store, roles, api, auth, host, node_state, proc
from vl.lib.cli import HelpOnErrorGroup
from vl.lib.output import is_json, render, note, warn
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
        "pid": node.pid,
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


def create_node_dir(org: str, n: int) -> None:
    node_root = Path.home() / "orgs" / org / "nodes" / f"node{n}"
    node_bin_dir = node_root / "bin"
    node_etc_dir = node_root / "etc"
    node_data_dir = node_root / "data"
    node_identity_dir = node_root / "identity"
    node_lib_dir = node_root / "lib"
    node_snapshots_dir = node_root / "snapshots"
    node_root.mkdir(parents=True)
    node_bin_dir.mkdir()
    node_etc_dir.mkdir()
    node_data_dir.mkdir()
    node_identity_dir.mkdir()
    node_lib_dir.mkdir()
    node_snapshots_dir.mkdir()
    note(f"Created: {node_root}")


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

        node_root = Path.home() / "orgs" / org_name / "nodes" / f"node{org_node}"
        if node_root.exists():
            raise CliError(f"{node_root} already exists — remove it manually before creating this node.")

        _, identity_id = svc_acct.create_svc_acct(display_name, f"svc-acct for {display_name}", client_secret,
                            environment, identity_label, org_name, caller, role)
        create_node_dir(org_name, org_node)
        _node = store.add_node(environment.name, org_name, org_node, identity_id, resolved_port)
    render(_node_row(_node), title="Node created")


def node_root_dir(org: str, n: int) -> Path:
    return Path.home() / "orgs" / org / "nodes" / f"node{n}"


def node_executable(node: store.Node) -> Path:
    """The binary a process must be running to count as this node's own (§4c)."""
    return node_root_dir(node.org_name, node.org_node) / "bin" / "verilock"


def is_our_process(node: store.Node) -> bool:
    return proc.is_our_process(node.pid, node_executable(node))


def repair(node: store.Node) -> store.Node:
    """§4c repair on read: a ``RUNNING`` row whose pid isn't this node's own live
    process becomes ``STOPPED`` with no pid. Persisted, local, no control-plane call.
    """
    if node.node_state == "RUNNING" and not is_our_process(node):
        return store.set_node_stopped(node)
    return node


def is_node_down(node: store.Node) -> bool:
    if node.node_state == "RUNNING" and is_our_process(node):
        note(f"{node.org_name} node{node.org_node} is already running (pid {node.pid})")
        return False
    return True


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
    note(f"Started {org}/node{org_node} (pid: {proc.pid}).")


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

        if is_node_down(node):
            node_root = populate_node_dir(environment, org_name, org_node, node)
            start_verilock(node, node_root, org_name, org_node)


def shutdown_node(node: store.Node) -> bool:
    """SIGTERM, poll, then SIGKILL. Only call this for a pid that passed :func:`is_our_process`.

    Polls with the same §4c check, so a zombie or a pid that's been reused counts
    as gone and is never signalled again.
    """
    assert node.pid is not None
    pid = node.pid
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        note(f"{pid} was not running or was already terminated")
        return True

    deadline = time.time() + 10.0
    while time.time() < deadline:
        if not is_our_process(node):
            note(f"Stopped {node.org_name}/node{node.org_node} (pid: {pid}).")
            return True
        time.sleep(0.25)

    # Still alive after the timeout: SIGKILL, then a brief pause for the kernel to reap.
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        note(f"Terminated {node.org_name}/node{node.org_node} (pid: {pid}).")
        return True
    except PermissionError:
        pass
    time.sleep(0.1)

    if not is_our_process(node):
        note(f"Terminated {node.org_name}/node{node.org_node} (pid: {pid}).")
        return True

    note(f"Failure to shutdown {node.org_name}/node{node.org_node} (pid: {pid}).")
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
            note(f"{org_name}/node{node.org_node} is not running.")
        elif is_our_process(node):
            if shutdown_node(node):
                store.set_node_stopped(node)
        else:
            store.set_node_stopped(node)
            note("done")

def fetch_node_by_id(environment: store.Environment, node: store.Node) -> dict[str, Any] | None:
    """`GET /v1/nodes/{id}` """
    acct = store.get_svc_acct(node.svc_acct_id)
    assert acct is not None
    assert acct.client_id is not None
    identity = store.get_identity_by_server_id(environment.name, acct.client_id,"SERVICE_ACCOUNT")
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
    node_dto = fetch_node_by_id(environment, node)
    assert node_dto is not None
    state: str = node_dto["state"]
    return state != "DOWN"


def reset_node(node_root: Path, include_snapshots: bool) -> None:
    data_dir = node_root / "data"
    shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    if include_snapshots:
        snapshots_dir = node_root / "snapshots"
        shutil.rmtree(snapshots_dir, ignore_errors=True)
        snapshots_dir.mkdir(parents=True, exist_ok=True)


@app.command("reset")
def reset(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    org_node: Annotated[int, typer.Argument(help="Node number to reset")],
    *,
    include_snapshots: Annotated[
        bool,
        typer.Option("--include-snapshots", help="clear the snapshots directory along with the data directory."),
    ] = False,
    yes: Annotated[
        bool | None,
        typer.Option("--yes", help="Skip the confirmation prompt."),
    ] = None,
    env: EnvOption = None
    ) -> None:
    """Reset the node """
    with report_errors():
        environment = store.get_environment(env)
        org_name = store.get_organization(environment.name, org).name
        node = store.get_node(environment.name, org_name, org_node)
        if node.node_state != "NEW" and node_is_up(environment, node):
            note(f"{org_name}/node{org_node} is still reported UP by the control plane — stop it first or try again.")
            return
        if yes is None:
            if not sys.stdin.isatty():
                raise CliError("vl node reset requires confirmation; pass --yes to run non-interactively")
            if include_snapshots:
                prompt_prefix = f"Reset {org_name}/node{org_node}'s data and snapshots directories?"
            else:
                prompt_prefix = f"Reset {org_name}/node{org_node}'s data directory?"
            yes = typer.confirm(
                f"{prompt_prefix} This permanently deletes all local blockchain state. Are you sure?",
                default=False,
            )

        if yes:
            node_root = Path.home() / "orgs" / org_name / "nodes" / f"node{org_node}"
            reset_node(node_root, include_snapshots)
            if include_snapshots:
                msg_suffix = "data and snapshots directories."
            else:
                msg_suffix = "data directory."
            note(f"Deleted {org_name}/node{org_node}'s {msg_suffix}")


# --------------------------------------------------------------------------- #
# list / show: local rows, repaired, with the control plane's view overlaid (§5.5)
# --------------------------------------------------------------------------- #

CP_UNREACHABLE_WARNING = "warning: control plane unreachable; showing local state only"


def _scope_org_names(environment: store.Environment, caller: store.Identity) -> set[str] | None:
    """The orgs whose nodes ``caller`` may list (§5.5 step 2); ``None`` means every org.

    A user's cached ``org_membership`` rows, or every org if one of them is
    platform-wide; a service account's own org. Mirrors what the CP returns for
    the same identity, so both views cover the same nodes.
    """
    if caller.kind == "SERVICE_ACCOUNT":
        sa = store.get_svc_acct(caller.id)
        return {sa.org_name} if sa is not None else set()
    memberships = [
        m for m in store.list_org_memberships(caller.id) if m.environment_name == environment.name
    ]
    if any(m.role == roles.OrgRole.PLATFORM_ADMIN.value for m in memberships):
        return None
    return {m.org_name for m in memberships}


def _in_scope(node: store.Node, scope: set[str] | None) -> bool:
    return scope is None or node.org_name in scope


def _fetch_cp_nodes(
    environment: store.Environment, caller: store.Identity
) -> list[dict[str, Any]] | None:
    """Every node the CP shows ``caller`` (list form, no ``scope=all``), paged to the end.

    ``None`` when the CP is unreachable (connection failure, timeout, ``5xx``),
    after warning. Any other error, ``401``/``403`` included, is raised: a
    rejection means this caller isn't entitled to the view, so there is no
    falling back to local rows.
    """

    def page_all(client: api.AppClient) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            body: dict[str, Any] = client.get(
                "/v1/nodes", params={"cursor": cursor} if cursor else None
            )
            items.extend(body.get("items") or [])
            cursor = body.get("nextCursor")
            if not cursor:
                return items

    try:
        return auth.authed_call(caller, environment.idp_base_url, page_all, environment.cp_base_url)
    except api.ApiError as exc:
        if exc.status_code == 0 or exc.status_code >= 500:
            warn(CP_UNREACHABLE_WARNING)
            return None
        raise


def _cp_by_node(
    environment: store.Environment, cp_rows: list[dict[str, Any]] | None
) -> dict[tuple[str, int], node_state.CpNode] | None:
    """CP rows keyed by ``(org name, org_node)``, via ``organization.server_org_id``."""
    if cp_rows is None:
        return None
    org_names = {o.server_org_id: o.name for o in store.list_organizations(environment.name)}
    by_node: dict[tuple[str, int], node_state.CpNode] = {}
    for dto in cp_rows:
        name = org_names.get(str(dto.get("orgId")))
        org_node = dto.get("orgNode")
        if name is None or not isinstance(org_node, int):
            continue  # an org this store has never cached has no local rows to match
        by_node[(name, org_node)] = node_state.cp_node_from_dto(dto)
    return by_node


class _Overlay:
    """Computes displayed states, gathering this machine's addresses at most once."""

    def __init__(self, cp: dict[tuple[str, int], node_state.CpNode] | None) -> None:
        self._cp = cp
        self._local: set[str] | None = None

    def cp_for(self, node: store.Node) -> node_state.CpNode | None:
        return None if self._cp is None else self._cp.get((node.org_name, node.org_node))

    def state(self, node: store.Node) -> str:
        cp = self.cp_for(node)
        if cp is None:
            return node.node_state
        if self._local is None:
            self._local = host.local_identities()
        return node_state.displayed_state(
            node.node_state,
            node.pid,
            cp,
            on_this_machine=host.is_this_machine(cp.host, self._local),
            cp_pid_alive=proc.is_cp_process(cp.pid),
            local_age_seconds=proc.process_age_seconds(node.pid) if node.node_state == "RUNNING" else None,
        )


@app.command("list")
def list_(
    org: Annotated[
        str | None,
        typer.Option(
            "--org",
            help="Filter to nodes based on org."
        ),
    ] = None,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """List this machine's nodes, reconciled with the control plane's view."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        scope = _scope_org_names(environment, caller)
        if org is not None:
            organization = store.get_organization(environment.name, org)
            local = store.get_nodes(environment, organization)
            title = f"nodes in {environment.name}-{organization.name}"
        else:
            local = store.get_nodes(environment)
            title = f"nodes in {environment.name}"
        nodes = [repair(n) for n in local if _in_scope(n, scope)]

        overlay = _Overlay(_cp_by_node(environment, _fetch_cp_nodes(environment, caller)))
        rows = [
            {
                "org": n.org_name,
                "node": n.org_node,
                "port": n.port,
                "state": overlay.state(n),
                "pid": n.pid,
                "created_at": n.created_at,
            }
            for n in nodes
        ]
        render(rows, title=title)


@app.command("show")
def show(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    org_node: Annotated[int, typer.Argument(help="Node number to show")],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Show one node, reconciled with the control plane's view (same as list)."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        org_name = store.get_organization(environment.name, org).name
        not_found = store.NodeNotFoundError(
            f"No node {org_name}/node{org_node} in environment {environment.name!r} "
            f"visible to {caller.label!r}."
        )
        try:
            local = store.get_node(environment.name, org_name, org_node)
        except store.NodeNotFoundError:
            raise not_found from None
        if not _in_scope(local, _scope_org_names(environment, caller)):
            raise not_found
        node = repair(local)

        # The org-scoped list form, filtered here to this (org, org_node): the
        # orgNode form derives org_id from a single-org token, which a user's isn't.
        overlay = _Overlay(_cp_by_node(environment, _fetch_cp_nodes(environment, caller)))
        state = overlay.state(node)
        cp = overlay.cp_for(node)
        reason = (
            node_state.explain(node.node_state, node.pid, cp)
            if cp is not None and state != node.node_state
            else None
        )

        acct = store.get_svc_acct(node.svc_acct_id)
        assert acct is not None
        row: dict[str, Any] = {
            "org_node": node.org_node,
            "port": node.port,
            "pid": node.pid,
            "state": state,
            "created_at": node.created_at,
            "client_id": acct.client_id,
            "private_key_path": acct.private_key_path,
            "public_key_path": acct.public_key_path,
        }
        if reason is not None and is_json():
            row["reason"] = reason
        render(row, title=f"Org: {org_name}, Node: {org_node}")
        if reason is not None:
            note(reason)
