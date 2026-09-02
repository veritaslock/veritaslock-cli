"""`vl org` — manage VeritasLock organizations.

Reads (`show`, `list`) are unauthenticated. Writes authenticate as the resolved
identity (`--as` / `VL_IDENTITY` / the environment default), same as every other
resource command.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from vl.commands._shared import AsOption, EnvOption, report_errors, resolve_org
from vl.lib import api, auth, store
from vl.lib.output import console, render
from vl.lib.roles import OrgRole

app = typer.Typer(help="Manage VeritasLock organizations.", no_args_is_help=True)
members_app = typer.Typer(
    help="Manage an organization's members.", no_args_is_help=True
)
app.add_typer(members_app, name="members")


def _org_row(org: store.Organization) -> dict[str, str]:
    return {
        "name": org.name,
        "display_name": org.display_name,
        "server_org_id": org.server_org_id,
        "active": "yes" if org.active else "no",
        "synced_at": org.synced_at,
    }


def _cache_from_dto(environment_name: str, dto: dict[str, Any]) -> store.Organization:
    return store.upsert_organization(
        environment_name,
        dto["name"],
        dto["id"],
        dto["displayName"],
        bool(dto["active"]),
    )


def _mirror_membership(
    environment_name: str, user_id: str, org_name: str, role: str
) -> None:
    """Keep a locally-cached user's org_membership row in step with the server."""
    local = store.get_identity_by_server_id(environment_name, user_id, "USER")
    if local is not None:
        store.upsert_org_membership(local.id, environment_name, org_name, role)


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
        with api.IdpClient(environment.idp_base_url) as client:
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
        with api.IdpClient(environment.idp_base_url) as client:
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
        console.print(f"[dim]more results — rerun with --page {next_cursor}[/dim]")


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


@members_app.command("add")
def members_add(
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


@members_app.command("list")
def members_list(
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


@members_app.command("set-role")
def members_set_role(
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


@members_app.command("remove")
def members_remove(
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
