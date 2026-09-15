"""`vl node` — manage VeritasLock nodes.

Authenticates with Phase 3's `--as` / `VL_IDENTITY` / default identity + cached
token flow throughout. See vl-node-spec.md.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer
import socket

from vl.commands._shared import (
    AsOption,
    EnvOption,
    cache_team,
    caller_orgs_claim,
    fetch_all_orgs,
    org_name_resolver,
    report_errors,
    resolve_org,
)
from vl.lib import auth, store
from vl.lib.cli import HelpOnErrorGroup
from vl.lib.output import console, render

app = typer.Typer(
    help="Manage VeritasLock nodes.", no_args_is_help=True, cls=HelpOnErrorGroup
)

def _node_row(dto: dict[str, Any], node: store.Node) -> dict[str, Any]:
    return {
        "org": node.org_name,
        "name": node.name,
        "host": dto["host"],
        "port": dto["port"],
        "status": node.node_status,
        "created_at": dto.get("createdAt", ""),
    }

def _resolve_node_name( org_name: str, node_name: str | None = None) -> str:
    if node_name is None:
        node_names = [obj.name for obj in store.list_nodes(org_name)]

        # Extract only the numeric part after the last hyphen
        nums = []
        for name in node_names:
            # Expect format: nodeN
            if name.startswith("node"):
                try:
                    nums.append(int(name[4:]))
                except ValueError:
                    pass

        max_n = max(nums) if nums else 0
        node_name = f"node{max_n + 1}"

    return f"{node_name}"


def _resolve_port(org_name: str, port: int | None = None) -> int:
    if port is None:
        ports = [obj.port for obj in store.list_nodes(org_name)]
        max_port = max(ports) if ports else 7000
        port = max_port + 1
    return port

def _resolve_host(org_name: str, host: str | None = None) -> str:
    if host is None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Doesn't need to be reachable — just used to pick the right interface
            s.connect(("8.8.8.8", 80))
            host = s.getsockname()[0]
        finally:
            s.close()

    return host

@app.command("create")
def create(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    name: Annotated[
        str | None,
        typer.Option("--name", help="Node name (default: org-name-nodeN."),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option("--port", help="Node port (default: next available port)."),
    ] = None,
    host: Annotated[
        str | None,
        typer.Option( "--host", help="Node host (default: ip address of localhost)."),
    ] = None,

    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Create a node """
    with report_errors():
        environment = store.get_environment(env)
        node_name = _resolve_node_name(org, name)
        port = _resolve_port(org, port)
        host = _resolve_host(org, host)
        caller = store.resolve_identity(environment.name, as_)
        org_dto = resolve_org(environment, org)
        org_name = str(org_dto["name"])
        dto = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.post(
                "/v1/nodes",
                json={"orgId": org_dto["id"], "port": port, "host": host},
            ),
        )
        node = store.add_node(environment.name, org_name, node_name, host, 0, port)

    render(_node_row(dto, node), title="Node created")


