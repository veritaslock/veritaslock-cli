"""`vl org` — manage VeritasLock organizations.

Reads (`show`, `list`) are unauthenticated. Writes authenticate as the resolved
identity (`--as` / `VL_IDENTITY` / the environment default), same as every other
resource command.
"""

from __future__ import annotations

import sys
import time
from typing import Annotated, Any

import typer

from vl.commands import node
from vl.commands._shared import AsOption, EnvOption, report_errors, CliError, failure_reason, require_operator
from vl.lib import api, auth, store
from vl.lib.cli import HelpOnErrorGroup
from vl.lib.output import console, note, render
from vl.lib.store import Environment, Node, Organization

app = typer.Typer(
    help="Manage VeritasLock organizations.",
    no_args_is_help=True,
    cls=HelpOnErrorGroup,
)


def _org_row(org: store.Organization) -> dict[str, str]:
    return {
        "name": org.name,
        "display_name": org.display_name,
        "server_org_id": org.server_org_id,
        "active": "yes" if org.active else "no",
        "synced_at": org.synced_at,
        "created_at": org.created_at,
    }


def _cache_from_dto(environment_name: str, dto: dict[str, Any]) -> store.Organization:
    return store.upsert_organization(
        environment_name,
        dto["name"],
        dto["id"],
        dto["displayName"],
        bool(dto["active"]),
        dto["createdAt"],
    )


# --------------------------------------------------------------------------- #
# reads (unauthenticated)
# --------------------------------------------------------------------------- #


@app.command("show")
def show(
    name: Annotated[str, typer.Argument(help="Organization name.")],
    env: EnvOption = None,
) -> None:
    """Show an organization."""
    with report_errors():
        environment = store.get_environment(env)
        with api.AppClient(environment.idp_base_url) as client:
            dto = client.get("/v1/organizations", params={"name": name})
        org = _cache_from_dto(environment.name, dto)
    render(_org_row(org), title=f"Organization: {name}")


@app.command("list")
def list_(
    active: Annotated[
        bool | None,
        typer.Option("--active/--no-active", help="Filter by active status."),
    ] = None,
    page: Annotated[
        int, typer.Option("--page", min=0, help="Page number (0-based).")
    ] = 0,
    env: EnvOption = None,
) -> None:
    """List organizations."""
    with report_errors():
        environment = store.get_environment(env)
        params: dict[str, Any] = {"page": page}
        if active is not None:
            params["active"] = str(active).lower()
        with api.AppClient(environment.idp_base_url) as client:
            body = client.get("/v1/organizations", params=params)
        items: list[dict[str, Any]] = body.get("items", [])
        for item in items:
            _cache_from_dto(environment.name, item)
        next_cursor = body.get("nextCursor")

    render(
        [
            {
                "name": item["name"],
                "display_name": item["displayName"],
                "server_org_id": item["id"],
                "active": "yes" if item["active"] else "no",
            }
            for item in items
        ],
        title="Organizations",
    )
    if next_cursor:
        note(f"more results — rerun with --page {next_cursor}")


# --------------------------------------------------------------------------- #
# writes (authenticated as the resolved identity)
# --------------------------------------------------------------------------- #


@app.command("add")
def add(
    name: Annotated[
        str, typer.Argument(help="Canonical org name (sent to the server as-is).")
    ],
    display_name: Annotated[
        str, typer.Option("--display-name", help="Human-readable display name.")
    ],
    initial_admin: Annotated[
        str | None,
        typer.Option(
            "--initial-admin",
            help="Server user id of an existing user to make ORG_ADMIN + owner "
            "instead of the caller. Must already exist.",
        ),
    ] = None,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Create an organization."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        body: dict[str, Any] = {"name": name, "displayName": display_name}
        if initial_admin is not None:
            body["initialAdminUserId"] = initial_admin
        dto = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.post("/v1/organizations", json=body),
        )
        org = _cache_from_dto(environment.name, dto)
    render(_org_row(org), title="Organization created")


@app.command("update")
def update(
    name: Annotated[str, typer.Argument(help="Organization name.")],
    active: Annotated[
        bool | None,
        typer.Option("--active/--no-active", help="Set the active flag."),
    ] = None,
    owner: Annotated[
        str | None,
        typer.Option("--owner", help="Server-side user id of the new owner."),
    ] = None,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Update an organization's active flag and/or owner."""
    if active is None and owner is None:
        console.print(
            "[red]Error:[/red] nothing to update — supply --active/--no-active "
            "and/or --owner."
        )
        raise typer.Exit(1)
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        body: dict[str, Any] = {}
        if active is not None:
            body["active"] = active
        if owner is not None:
            body["ownerId"] = owner
        dto = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.patch("/v1/organizations", params={"name": name}, json=body),
        )
        org = _cache_from_dto(environment.name, dto)
    render(_org_row(org), title="Organization updated")


@app.command("start")
def start(
    org: Annotated[
        str, typer.Argument(help="Canonical org name (sent to the server as-is).")
    ],
    *,
    env: EnvOption = None,
    as_: AsOption = None) -> None:
    """Start all nodes in an organization. Needs ORG_ADMIN of the org, or PLATFORM_ADMIN."""
    with report_errors():
        environment = store.get_environment(env)
        caller = require_operator(environment, as_, org)
        organization = store.get_organization(environment.name, org)
        nodes = store.get_nodes(environment, organization)
        failures: list[tuple[str, str]] = []
        for node_ in nodes:
            try:
                node.start(organization.name, node_.org_node, env=environment.name, as_=caller.label)
            except Exception as e:  # keep starting the rest; node.start printed its own error
                failures.append((f"{organization.name}/node{node_.org_node}", failure_reason(e)))
        if failures:
            raise OrgStartFailed(organization.name, len(nodes), failures)


class OrgStartFailed(CliError):
    """Some of an org's nodes failed to start; ``failures`` is ``[(org/nodeN, reason)]``."""

    def __init__(self, org_name: str, total: int, failures: list[tuple[str, str]]) -> None:
        self.failures = failures
        lines = [f"{len(failures)} of {total} nodes in {org_name} failed to start:"]
        lines += [f"  {label}: {reason}" for label, reason in failures]
        super().__init__("\n".join(lines))


def stop_nodes(environment: Environment, nodes: list[Node], organization: Organization,
               caller: store.Identity) -> None:
    """Stop each node, as ``caller``, who each node command re-checks.

    The poll for DOWN still authenticates as each node's own service account
    (``node.node_is_up``): the operator identity only gates the commands.
    """
    nodes.reverse()
    for idx, node_ in enumerate(nodes):
        try:
            is_last = idx == len(nodes) - 1
            if is_last and organization.name == "globo":
                time.sleep(2)
            elif idx != 0:
                time.sleep(0.5)

            node.stop(organization.name, node_.org_node, env=environment.name, as_=caller.label)
            cnt = 0
            node_stopped = False
            while cnt < 20:
                time.sleep(.25)
                if not node.node_is_up(environment, node_):
                    node_stopped = True
                    break
                cnt += 1

            if not node_stopped:
                note (f"Warning: {organization.name}-node{node_.org_node} is still reported UP by the control plane.")

        except Exception as e:
            note(f"Warning: {e}")

@app.command("stop")
def stop(
    org: Annotated[
        str, typer.Argument(help="Canonical org name (sent to the server as-is).")
    ],
    *,
    env: EnvOption = None,
    as_: AsOption = None) -> None:
    """Stop all nodes in an organization. Needs ORG_ADMIN of the org, or PLATFORM_ADMIN."""
    with report_errors():
        environment = store.get_environment(env)
        caller = require_operator(environment, as_, org)
        organization = store.get_organization(environment.name, org)
        nodes = store.get_nodes(environment, organization)
        stop_nodes(environment, nodes, organization, caller)


def nodes_not_down(environment: Environment, nodes: list[Node]) -> list[tuple[str, str]]:
    """``[(org/nodeN, state)]`` for each node the control plane doesn't report DOWN.

    One state check per node, as its own service account, the same single call
    ``vl node reset`` makes. A NEW node has never registered, so it isn't checked.
    """
    not_down: list[tuple[str, str]] = []
    for node_ in nodes:
        if node_.node_state == "NEW":
            continue
        dto = node.fetch_node_by_id(environment, node_)
        assert dto is not None
        state: str = dto["state"]
        if state != "DOWN":
            not_down.append((f"{node_.org_name}/node{node_.org_node}", state))
    return not_down


class OrgResetRefused(CliError):
    """Some of an org's nodes aren't DOWN, so nothing was reset."""

    def __init__(self, org_name: str, not_down: list[tuple[str, str]]) -> None:
        lines = [f"Can't reset org '{org_name}': every node must be down, and these are not:"]
        lines += [f"  {label} ({state})" for label, state in not_down]
        lines.append(f"Run `vl org stop {org_name}` first.")
        super().__init__("\n".join(lines))


class OrgResetIncomplete(CliError):
    """Some of an org's nodes weren't reset; ``failures`` is ``[(org/nodeN, reason)]``."""

    def __init__(self, org_name: str, total: int, failures: list[tuple[str, str]]) -> None:
        self.failures = failures
        lines = [f"{len(failures)} of {total} nodes in {org_name} were not reset:"]
        lines += [f"  {label}: {reason}" for label, reason in failures]
        super().__init__("\n".join(lines))


@app.command("reset")
def reset(
    org: Annotated[
        str, typer.Argument(help="Canonical org name (sent to the server as-is).")
    ],
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
    as_: AsOption = None) -> None:
    """Reset all nodes in an organization. Needs ORG_ADMIN of the org, or PLATFORM_ADMIN.

    Every node must already be down (run `vl org stop` first); this stops nothing.
    If any node is not down, nothing is reset.
    """
    with report_errors():
        environment = store.get_environment(env)
        caller = require_operator(environment, as_, org)
        organization = store.get_organization(environment.name, org)
        org_name = organization.name
        nodes = store.get_nodes(environment, organization)
        nodes.reverse()
        not_down = nodes_not_down(environment, nodes)
        if not_down:
            raise OrgResetRefused(org_name, not_down)
        if not yes:
            if not sys.stdin.isatty():
                raise CliError("vl org reset requires confirmation; pass --yes to run non-interactively")
            if include_snapshots:
                prompt_prefix = f"Reset the data and snapshots directories of ALL nodes in org '{org_name}'?"
            else:
                prompt_prefix = f"Reset ALL nodes in org '{org_name}'?"
            yes = typer.confirm(
                f"{prompt_prefix} This permanently deletes all local blockchain state. Are you sure (y/N)?",
                default=False,
                show_default=False,
                prompt_suffix=" ",
            )

        if yes:
            failures: list[tuple[str, str]] = []
            for node_ in nodes:
                label = f"{org_name}/node{node_.org_node}"
                try:
                    if not node.reset(org_name, node_.org_node, include_snapshots=include_snapshots, yes=True,
                                      env=environment.name, as_=caller.label):
                        failures.append((label, "skipped: reported UP by the control plane when its turn came"))
                except Exception as e:  # keep resetting the rest; node.reset printed its own error
                    failures.append((label, failure_reason(e)))
            if failures:
                raise OrgResetIncomplete(org_name, len(nodes), failures)
