"""`vl node` — manage VeritasLock nodes.

Authenticates with Phase 3's `--as` / `VL_IDENTITY` / default identity + cached
token flow throughout. See vl-node-spec.md.
"""

from __future__ import annotations

import secrets
import sys
from typing import Annotated, Any

import typer
from rich.markup import escape

from vl.commands import svc_acct
from vl.commands._shared import (
    AsOption,
    EnvOption,
    report_errors,
    resolve_org, assert_label_free, CliError, require_operator,
)
from vl.lib import store, roles, api, auth, host, node_state, nodelock, nodeops, proc
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


def create_node_dir(org: str, n: int) -> None:
    node_root = nodeops.node_root(org, n)
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

        if port is not None and (owner := store.get_node_by_port(port)) is not None:
            raise store.PortInUseError(
                f"Port {port} is already used by {owner.org_name}/node{owner.org_node} "
                f"(environment {owner.environment_name!r}). Pick another --port, or leave it out."
            )
        caller = store.resolve_identity(environment.name, as_)
        org_dto = resolve_org(environment, org)
        org_name = str(org_dto["name"])
        org_node = _resolve_org_node(environment.name, org_name, org_node)
        identity_label = f"{org_name}-{org_node}"
        display_name = f"{org_name} node{org_node}"
        role = roles.ServiceAccountRole.NODE
        client_secret = secrets.token_hex(16)
        assert_label_free(environment.name, identity_label)

        node_root = nodeops.node_root(org_name, org_node)
        if node_root.exists():
            raise CliError(f"{node_root} already exists — remove it manually before creating this node.")

        _, identity_id = svc_acct.create_svc_acct(display_name, f"svc-acct for {display_name}", client_secret,
                            environment, identity_label, org_name, caller, role)
        create_node_dir(org_name, org_node)
        # With no --port, add_node allocates it with the insert (BEGIN IMMEDIATE).
        _node = store.add_node(environment.name, org_name, org_node, identity_id, port)
    render(_node_row(_node), title="Node created")


def node_root_dir(org: str, n: int) -> Path:
    return nodeops.node_root(org, n)


class NodeStartFailed(CliError):
    """The node's process exited right after `vl node start` spawned it."""

    def __init__(self, label: str, returncode: int, log_path: Path) -> None:
        tail = nodeops.read_tail(log_path)
        self.label = label
        self.reason = nodeops.start_failure_reason(returncode)
        lines = [f"{label} {self.reason}."]
        hint = nodeops.start_failure_hint(returncode, tail or [])
        if hint:
            lines.append(hint)
        lines.append("---- Log tail ----")
        if tail is None:
            lines.append(f"(log file {log_path} not found)")
        else:
            lines.extend(tail)
        # Log lines can hold [brackets] that Rich would read as markup.
        super().__init__("\n".join(escape(line) for line in lines))


def start_failure_error(org: str, org_node: int, result: nodeops.NodeResult) -> CliError:
    """The error `vl node start` reports for a ``FAILED`` start: an early exit
    with its hint and log tail, or the result's detail (a busy node)."""
    label = f"{org}/node{org_node}"
    if result.exit_code is not None:
        return NodeStartFailed(label, result.exit_code, nodeops.log_path(org, org_node))
    return CliError(f"{label}: {result.detail}")


@app.command("start")
def start(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    org_node: Annotated[int, typer.Argument(help="Node number to start")],
    *,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Start the node. Needs ORG_ADMIN of the node's org, or PLATFORM_ADMIN."""
    with report_errors():
        environment = store.get_environment(env)
        require_operator(environment, as_, org)
        org_name = store.get_organization(environment.name, org).name
        store.get_node(environment.name, org_name, org_node)  # no such node: the store's error
        result = nodeops.start_node(environment, org_name, org_node, note)
        if result.outcome is nodeops.Outcome.FAILED:
            raise start_failure_error(org_name, org_node, result)


@app.command("stop")
def stop(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    org_node: Annotated[int, typer.Argument(help="Node number to stop")],
    *,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Stop the node. Needs ORG_ADMIN of the node's org, or PLATFORM_ADMIN."""
    with report_errors():
        environment = store.get_environment(env)
        require_operator(environment, as_, org)
        org_name = store.get_organization(environment.name, org).name
        store.get_node(environment.name, org_name, org_node)
        result = nodeops.stop_node(environment, org_name, org_node, note)
        # A node that would not die has had its own line ("Failure to shutdown …"),
        # and exits 0 as it always has; a busy node is an error.
        if result.detail == nodelock.BUSY_DETAIL:
            raise CliError(f"{org_name}/node{org_node}: {result.detail}")


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
    env: EnvOption = None,
    as_: AsOption = None,
) -> bool:
    """Reset the node. Needs ORG_ADMIN of the node's org, or PLATFORM_ADMIN."""
    with report_errors():
        environment = store.get_environment(env)
        require_operator(environment, as_, org)
        org_name = store.get_organization(environment.name, org).name
        node = store.get_node(environment.name, org_name, org_node)
        if yes is None:
            # The check comes before the prompt, so an up node is reported without
            # one; reset_node checks again under the node's lock.
            if node.node_state != "NEW" and nodeops.node_is_up(environment, node):
                note(f"{org_name}/node{org_node} is still reported UP by the control plane — stop it first or try again.")
                return False
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

        if not yes:
            return False
        result = nodeops.reset_node(
            environment, org_name, org_node, note, include_snapshots=include_snapshots
        )
        if result.detail == nodelock.BUSY_DETAIL:
            raise CliError(f"{org_name}/node{org_node}: {result.detail}")
        return result.outcome is nodeops.Outcome.RESET


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
        nodes = [nodeops.repair(n) for n in local if _in_scope(n, scope)]

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
        node = nodeops.repair(local)

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
