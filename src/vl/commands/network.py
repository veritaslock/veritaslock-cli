"""`vl network` — manage VeritasLock network.
`start` and `stop` are unauthenticated. 'status' and 'reset' are authenticated as the resolved
identity (`--as` / `VL_IDENTITY` / the environment default), same as every other
resource command.
"""

from __future__ import annotations

import sys
from typing import Annotated, Any

import typer

from vl.commands import org
from vl.commands._shared import AsOption, EnvOption, report_errors, CliError, org_name_resolver
from vl.lib import auth, store
from vl.lib.cli import HelpOnErrorGroup
from vl.lib.output import note, render
from vl.lib.store import Environment, Organization

app = typer.Typer(
    help="Manage VeritasLock network.",
    no_args_is_help=True,
    cls=HelpOnErrorGroup,
)


def curate_orgs(environment: Environment) -> list[Organization]:
    orgs = store.get_organizations(environment.name)
    orgs = [o for o in orgs if o.name != "veritaslock"]
    orgs.sort(key=lambda o: o.name != "globo")
    return orgs


@app.command("start")
def start(
    env: EnvOption = None) -> None:
    """Start all orgs in network."""
    with report_errors():
        environment = store.get_environment(env)

        orgs = curate_orgs(environment)
        for org_ in orgs:
            try:
                org.start(org_.name, environment.name)
            except Exception as e:
                note(f"Warning: {e}")



@app.command("stop")
def stop(
    env: EnvOption = None) -> None:
    """Stops all orgs in network."""
    with report_errors():
        environment = store.get_environment(env)

        orgs = curate_orgs(environment)
        orgs.reverse()
        for org_ in orgs:
            try:
                org.stop(org_.name, environment.name)
            except Exception as e:
                note(f"Warning: {e}")


def get_network_status(as_: str | None, env: str | None) -> list[dict[str, Any]]:
    environment = store.get_environment(env)
    caller = store.resolve_identity(environment.name, as_)
    body: dict[str, Any] = auth.authed_call(
        caller,
        environment.idp_base_url,
        lambda c: c.get(
            "/v1/nodes"
        ),
        environment.cp_base_url,
    )
    items: list[dict[str, Any]] = body.get("items", [])
    return items


def confirm_all_nodes_are_down(as_: str | None, env: str | None, environment: Environment) -> None:
    node_dtos = get_network_status(as_, env)

    nodes_not_down : list[str] = []
    resolve_org = org_name_resolver(environment)

    for dto in node_dtos:
        org_name = resolve_org(dto["orgId"])
        if dto["state"] != "DOWN":
            nodes_not_down.append(f"{org_name}/node{dto['orgNode']}")

    if nodes_not_down:
        node_list = ", ".join(nodes_not_down)
        raise CliError(
            f"Nodes still up: {node_list}. Please issue a vl network stop first."
        )


@app.command("reset")
def reset(
    yes: Annotated[
        bool | None,
        typer.Option("--yes", help="reset all nodes in org by clearing node's data directory."),
    ] = None,
    env: EnvOption = None,
    as_: AsOption = None) -> None:
    """Resets all orgs in network."""
    with report_errors():
        environment = store.get_environment(env)
        confirm_all_nodes_are_down(as_, env, environment)
        if not yes:
            if not sys.stdin.isatty():
                raise CliError("vl org reset requires confirmation; pass --yes to run non-interactively")
            yes = typer.confirm(
                f"Reset ALL nodes in {environment.name}? "
                f"This permanently deletes local blockchain state for every node. Are you sure?",
                default=False,
            )

        if yes:
            orgs = curate_orgs(environment)
            orgs.reverse()
            for org_ in orgs:
                try:
                    org.reset(org_.name, True, environment.name)
                except Exception as e:
                    note(f"Warning: {e}")



@app.command("status")
def status(
    env: EnvOption = None,
    as_: AsOption = None) -> None:
    with report_errors():
        environment = store.get_environment(env)
        node_dtos = get_network_status(as_, env)
        resolve_org = org_name_resolver(environment)
        rows = [
            {
                "org": resolve_org(dto["orgId"]),
                "org_node": dto["orgNode"],
                "state": dto["state"],
                "host": dto.get("host"),
            }
            for dto in node_dtos
        ]
        render(rows, title=f"nodes in {environment.name}")