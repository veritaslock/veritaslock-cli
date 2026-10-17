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
from vl.commands._shared import AsOption, EnvOption, report_errors, CliError, org_name_resolver, require_operator
from vl.lib import auth, store, api
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
    env: EnvOption = None,
    as_: AsOption = None) -> None:
    """Start all orgs in network. Needs PLATFORM_ADMIN."""
    with report_errors():
        environment = store.get_environment(env)
        caller = require_operator(environment, as_)

        orgs = curate_orgs(environment)
        for org_ in orgs:
            try:
                org.start(org_.name, env=environment.name, as_=caller.label)
            except Exception as e:
                note(f"Warning: {e}")



@app.command("stop")
def stop(
    env: EnvOption = None,
    as_: AsOption = None) -> None:
    """Stops all orgs in network. Needs PLATFORM_ADMIN."""
    with report_errors():
        environment = store.get_environment(env)
        caller = require_operator(environment, as_)

        orgs = curate_orgs(environment)
        orgs.reverse()
        for org_ in orgs:
            try:
                org.stop(org_.name, env=environment.name, as_=caller.label)
            except Exception as e:
                note(f"Warning: {e}")


def get_network_status(as_: str | None, env: str | None) -> list[dict[str, Any]]:
    environment = store.get_environment(env)
    caller = store.resolve_identity(environment.name, as_)
    body: dict[str, Any] = auth.authed_call(
        caller,
        environment.idp_base_url,
        lambda c: c.get(
            "/v1/nodes", params={"scope": "all"}
        ),
        environment.cp_base_url,
    )
    items: list[dict[str, Any]] = body.get("items", [])
    return items


def reset_cp_state(caller: store.Identity, environment: Environment, *, dry_run: bool) -> dict[str, Any]:

    try:
        body: dict[str, Any] = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.post(
                "/v1/network/reset", params={"dryRun": dry_run}
            ),
            environment.cp_base_url,
        )
    except api.ApiError as exc:
        if exc.status_code == 403:
            raise CliError("--as must be a PLATFORM_ADMIN user (a service account isn't accepted)")
        elif exc.status_code == 404:
            raise CliError("This environment doesn't allow network reset (the control plane's network.reset.supported isn't enabled).")
        elif exc.status_code == 409:
            nodes = exc.context.get("nodes", [])

            not_down = ", ".join(
                f"{n['orgName']}/node{n['orgNode']} ({n['state']})"
                for n in nodes
            )

            if not_down:
                raise CliError(f"The following nodes are not down: {not_down}")
            else:
                raise
        else:
            raise

    return body


@app.command("reset")
def reset(
    yes: Annotated[
        bool | None,
        typer.Option("--yes", help="Skip the confirmation prompt."),
    ] = None,
    env: EnvOption = None,
    as_: AsOption = None) -> None:
    """
    Clears control-plane study and block data and resets all nodes in network.
    Requires all nodes in the network to be down.
    """
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        if not yes:
            if not sys.stdin.isatty():
                raise CliError("vl network reset requires confirmation; pass --yes to run non-interactively")
            body = reset_cp_state(caller, environment, dry_run=True)
            counts = ", ".join(f"{table}: {n}" for table, n in body["deleted"].items() if n)
            if counts:
                deleted = f" ({counts}) "
            else:
                deleted = " "
            yes = typer.confirm(
                f"Reset ALL nodes in environment '{environment.name}'?\n"
                f"This permanently deletes the control plane's study and block data{deleted}"
                f"and the local blockchain state for every node, including its snapshots.\n"
                f"Are you sure?",
                default=False,
            )

        if yes:
            reset_cp_state(caller, environment, dry_run=False)
            orgs = curate_orgs(environment)
            orgs.reverse()
            for org_ in orgs:
                try:
                    org.reset(org_.name, include_snapshots=True, yes=True, env=environment.name,
                              as_=caller.label)
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