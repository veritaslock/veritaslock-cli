"""`vl org-members` — manage an organization's members. Peer to `vl org`.

Split out from the old `vl org members` sub-group for the same reason
`vl team-member` was split out of `vl team`: a resource and its membership are
different enough operations that they read better as siblings than as a nested
group. See `vl-command-restructure.md` for the mapping.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from vl.commands._shared import AsOption, EnvOption, report_errors, resolve_org
from vl.lib import auth, store
from vl.lib.cli import HelpOnErrorGroup
from vl.lib.output import console, render
from vl.lib.roles import OrgRole

app = typer.Typer(
    help="Manage an organization's members.",
    no_args_is_help=True,
    cls=HelpOnErrorGroup,
)


def _mirror_membership(
    environment_name: str, user_id: str, org_name: str, role: str
) -> None:
    """Keep a locally-cached user's org_membership row in step with the server."""
    local = store.get_identity_by_server_id(environment_name, user_id, "USER")
    if local is not None:
        store.upsert_org_membership(local.id, environment_name, org_name, role)


@app.command("add")
def add(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    user_id: Annotated[
        str, typer.Option("--user-id", help="Server-side user id to add.")
    ],
    role: Annotated[OrgRole, typer.Option("--role", help="Membership role.")],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Add a member to an organization."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        org_dto = resolve_org(environment, org)
        auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.post(
                f"/v1/organizations/{org_dto['id']}/members",
                json={"userId": user_id, "role": role.value},
            ),
        )
        _mirror_membership(environment.name, user_id, str(org_dto["name"]), role.value)
    console.print(
        f"Added user [bold]{user_id}[/bold] to [bold]{org}[/bold] as {role.value}."
    )


@app.command("list")
def list_(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """List an organization's members."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        org_dto = resolve_org(environment, org)
        body = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.get(f"/v1/organizations/{org_dto['id']}/members"),
        )
        rows: list[dict[str, Any]] = body.get("items", [])

    render(
        [
            {
                "orgId": row["orgId"],
                "userId": row["userId"],
                "role": row["role"],
                "addedAt": row.get("addedAt", ""),
            }
            for row in rows
        ],
        title=f"Members of {org}",
    )


@app.command("set-role")
def set_role(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    user_id: Annotated[str, typer.Argument(help="Server-side user id.")],
    role: Annotated[OrgRole, typer.Option("--role", help="New role for the member.")],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Change an existing member's role in place."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        org_dto = resolve_org(environment, org)
        auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.patch(
                f"/v1/organizations/{org_dto['id']}/members/{user_id}",
                json={"role": role.value},
            ),
        )
        _mirror_membership(environment.name, user_id, str(org_dto["name"]), role.value)
    console.print(
        f"Set user [bold]{user_id}[/bold]'s role in [bold]{org}[/bold] to "
        f"{role.value}."
    )


@app.command("remove")
def remove(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    user_id: Annotated[
        str, typer.Option("--user-id", help="Server-side user id to remove.")
    ],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Remove a member from an organization."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        org_dto = resolve_org(environment, org)
        auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.delete(
                f"/v1/organizations/{org_dto['id']}/members/{user_id}"
            ),
        )
    console.print(
        f"Removed user [bold]{user_id}[/bold] from [bold]{org}[/bold]."
    )
