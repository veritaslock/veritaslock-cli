"""`vl org` — manage VeritasLock organizations.

Reads (`show`, `list`) are unauthenticated. Writes (`add`, `update`, and every
`members` subcommand) authenticate with a one-off `POST /auth/user/login` using
`--auth-user` / `--auth-password` — nothing is cached to disk. Once the identity
store lands (Phase 3) these gain `--as <label>` instead. See vl-org-spec.md §5.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any

import typer

from vl.lib import api, store
from vl.lib.output import console, render
from vl.lib.roles import OrgRole

app = typer.Typer(help="Manage VeritasLock organizations.", no_args_is_help=True)
members_app = typer.Typer(
    help="Manage an organization's members.", no_args_is_help=True
)
app.add_typer(members_app, name="members")


EnvOption = Annotated[
    str | None,
    typer.Option("--env", help="Environment to target (default: the store's default)."),
]
AuthUserOption = Annotated[
    str, typer.Option("--auth-user", help="Username to authenticate the call as.")
]
AuthPasswordOption = Annotated[
    str,
    typer.Option(
        "--auth-password",
        prompt="Password",
        hide_input=True,
        help="Password for --auth-user (prompted if omitted).",
    ),
]


@contextmanager
def _report_errors() -> Iterator[None]:
    """Render a store or API error as one clean line + non-zero exit."""
    try:
        yield
    except store.StoreError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc
    except api.ApiError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        for field_error in exc.invalid_fields:
            console.print(f"  - {_fmt_field_error(field_error)}")
        if exc.correlation_id and exc.status_code >= 500:
            console.print(f"  [dim]correlation-id: {exc.correlation_id}[/dim]")
        raise typer.Exit(1) from exc


def _fmt_field_error(field_error: Any) -> str:
    if isinstance(field_error, dict):
        name = field_error.get("field") or field_error.get("name") or "?"
        message = field_error.get("message") or field_error.get("reason") or ""
        return f"{name}: {message}".rstrip(": ")
    return str(field_error)


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


def _resolve_org(
    client: api.IdpClient, environment_name: str, name: str
) -> dict[str, Any]:
    """Resolve an org by name against the server and refresh its local cache row."""
    dto: dict[str, Any] = client.get("/v1/organizations", params={"name": name})
    _cache_from_dto(environment_name, dto)
    return dto


# --------------------------------------------------------------------------- #
# Reads (unauthenticated)
# --------------------------------------------------------------------------- #


@app.command("show")
def show(
    name: Annotated[str, typer.Argument(help="Organization name.")],
    env: EnvOption = None,
) -> None:
    """Show an organization."""
    with _report_errors():
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
    with _report_errors():
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
# Writes (authenticated)
# --------------------------------------------------------------------------- #


@app.command("add")
def add(
    name: Annotated[
        str, typer.Argument(help="Canonical org name (sent to the server as-is).")
    ],
    display_name: Annotated[
        str, typer.Option("--display-name", help="Human-readable display name.")
    ],
    auth_user: AuthUserOption,
    auth_password: AuthPasswordOption,
    initial_admin: Annotated[
        str | None,
        typer.Option(
            "--initial-admin",
            help="Server user id of an existing user to make ORG_ADMIN + owner "
            "instead of the caller. Must already exist. For a brand-new dedicated "
            "admin, use the bootstrap sequence in vl-org-spec.md §4.8 instead.",
        ),
    ] = None,
    env: EnvOption = None,
) -> None:
    """Create an organization."""
    with _report_errors():
        environment = store.get_environment(env)
        token = api.login(environment.idp_base_url, auth_user, auth_password).access_token
        body: dict[str, Any] = {"name": name, "displayName": display_name}
        if initial_admin is not None:
            body["initialAdminUserId"] = initial_admin
        with api.IdpClient(environment.idp_base_url, token=token) as client:
            dto = client.post("/v1/organizations", json=body)
        org = _cache_from_dto(environment.name, dto)
    render(_org_row(org), title="Organization created")


@app.command("update")
def update(
    name: Annotated[str, typer.Argument(help="Organization name.")],
    auth_user: AuthUserOption,
    auth_password: AuthPasswordOption,
    active: Annotated[
        bool | None,
        typer.Option("--active/--no-active", help="Set the active flag."),
    ] = None,
    owner: Annotated[
        str | None,
        typer.Option("--owner", help="Server-side user id of the new owner."),
    ] = None,
    env: EnvOption = None,
) -> None:
    """Update an organization's active flag and/or owner."""
    if active is None and owner is None:
        console.print(
            "[red]Error:[/red] nothing to update — supply --active/--no-active "
            "and/or --owner."
        )
        raise typer.Exit(1)
    with _report_errors():
        environment = store.get_environment(env)
        token = api.login(environment.idp_base_url, auth_user, auth_password).access_token
        body: dict[str, Any] = {}
        if active is not None:
            body["active"] = active
        if owner is not None:
            body["ownerId"] = owner
        with api.IdpClient(environment.idp_base_url, token=token) as client:
            dto = client.patch(
                "/v1/organizations", params={"name": name}, json=body
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
    auth_user: AuthUserOption,
    auth_password: AuthPasswordOption,
    env: EnvOption = None,
) -> None:
    """Add a member to an organization."""
    with _report_errors():
        environment = store.get_environment(env)
        token = api.login(environment.idp_base_url, auth_user, auth_password).access_token
        with api.IdpClient(environment.idp_base_url, token=token) as client:
            org_dto = _resolve_org(client, environment.name, org)
            client.post(
                f"/v1/organizations/{org_dto['id']}/members",
                json={"userId": user_id, "role": role.value},
            )
        # Phase 3 §9.6 retrofit: if this user is one we hold a local identity for,
        # mirror the membership into org_membership. Otherwise there's nothing to
        # attach it to locally, and that's fine.
        local = store.get_identity_by_server_id(environment.name, user_id, "USER")
        if local is not None:
            store.upsert_org_membership(
                local.id, environment.name, str(org_dto["name"]), role.value
            )
    console.print(
        f"Added user [bold]{user_id}[/bold] to [bold]{org}[/bold] as {role.value}."
    )


@members_app.command("list")
def members_list(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    auth_user: AuthUserOption,
    auth_password: AuthPasswordOption,
    env: EnvOption = None,
) -> None:
    """List an organization's members."""
    with _report_errors():
        environment = store.get_environment(env)
        token = api.login(environment.idp_base_url, auth_user, auth_password).access_token
        with api.IdpClient(environment.idp_base_url, token=token) as client:
            org_dto = _resolve_org(client, environment.name, org)
            body = client.get(f"/v1/organizations/{org_dto['id']}/members")
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


@members_app.command("remove")
def members_remove(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    user_id: Annotated[
        str, typer.Option("--user-id", help="Server-side user id to remove.")
    ],
    auth_user: AuthUserOption,
    auth_password: AuthPasswordOption,
    env: EnvOption = None,
) -> None:
    """Remove a member from an organization."""
    with _report_errors():
        environment = store.get_environment(env)
        token = api.login(environment.idp_base_url, auth_user, auth_password).access_token
        with api.IdpClient(environment.idp_base_url, token=token) as client:
            org_dto = _resolve_org(client, environment.name, org)
            client.delete(
                f"/v1/organizations/{org_dto['id']}/members/{user_id}"
            )
    console.print(
        f"Removed user [bold]{user_id}[/bold] from [bold]{org}[/bold]."
    )
