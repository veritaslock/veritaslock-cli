"""`vl node` — manage VeritasLock nodes.

Authenticates with Phase 3's `--as` / `VL_IDENTITY` / default identity + cached
token flow throughout. See vl-node-spec.md.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any

import typer

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
        "status": node.node_status,
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
    path = Path.home() / "orgs" / org / "nodes" / f"node{n}"
    if path.exists():
        note(f"WARNING: Directory already exists: {path}")
    else:
        path.mkdir(parents=True)
        note(f"Created: {path}")
    return path


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


