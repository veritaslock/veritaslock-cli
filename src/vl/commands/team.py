"""`vl team` — manage VeritasLock teams, their members, and ingest clients.

Authenticates with Phase 3's `--as` / `VL_IDENTITY` / default identity + cached
token flow throughout. See vl-team-spec.md.
"""

from __future__ import annotations

import shutil
from typing import Annotated, Any

import typer

from vl.commands._shared import (
    AsOption,
    EnvOption,
    assert_label_free,
    report_errors,
    resolve_org,
    slugify,
)
from vl.lib import auth, store
from vl.lib.output import console, render
from vl.lib.roles import TeamRole

app = typer.Typer(help="Manage VeritasLock teams.", no_args_is_help=True)
members_app = typer.Typer(help="Manage a team's members.", no_args_is_help=True)
ingest_clients_app = typer.Typer(
    help="Manage a team's ingest clients.", no_args_is_help=True
)
app.add_typer(members_app, name="members")
app.add_typer(ingest_clients_app, name="ingest-clients")


def _cache_team(
    environment_name: str, org_name: str, dto: dict[str, Any]
) -> store.Team:
    return store.upsert_team(
        environment_name,
        org_name,
        str(dto["name"]),
        str(dto["id"]),
        description=dto.get("description"),
        created_by=dto.get("createdBy"),
        created_at=str(dto["createdAt"]) if dto.get("createdAt") else None,
    )


def _team_row(dto: dict[str, Any], team: store.Team | None) -> dict[str, Any]:
    return {
        "org": team.org_name if team else dto.get("orgId", ""),
        "name": dto["name"],
        "description": dto.get("description") or "",
        "server_team_id": dto["id"],
        "created_by": dto.get("createdBy", ""),
    }


def _resolve_team(
    caller: store.Identity, environment: store.Environment, org: str, team_name: str
) -> tuple[str, str]:
    """Return ``(server_team_id, org_name)`` — local cache first, then the server."""
    org_dto = resolve_org(environment, org)
    org_name = str(org_dto["name"])

    cached = store.get_team_or_none(environment.name, org_name, team_name)
    if cached is not None:
        return cached.server_team_id, org_name

    body = auth.authed_call(
        caller,
        environment.idp_base_url,
        lambda c: c.get(
            "/v1/teams", params={"orgId": org_dto["id"], "name": team_name}
        ),
    )
    items: list[dict[str, Any]] = body.get("items", [])
    if not items:
        raise store.TeamNotFoundError(
            f"No team {team_name!r} in organization {org!r}."
        )
    _cache_team(environment.name, org_name, items[0])
    return str(items[0]["id"]), org_name


# --------------------------------------------------------------------------- #
# teams
# --------------------------------------------------------------------------- #


@app.command("add")
def add(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    name: Annotated[str, typer.Argument(help="Team name.")],
    description: Annotated[
        str | None, typer.Option("--description", help="Team description.")
    ] = None,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Create a team (the caller becomes TEAM_ADMIN, server-side, in one transaction)."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        org_dto = resolve_org(environment, org)
        org_name = str(org_dto["name"])
        dto = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.post(
                "/v1/teams",
                json={"orgId": org_dto["id"], "name": name, "description": description},
            ),
        )
        team = _cache_team(environment.name, org_name, dto)
        store.upsert_team_member(
            caller.id, environment.name, org_name, name, "TEAM_ADMIN"
        )
    render(_team_row(dto, team), title="Team created")


@app.command("show")
def show(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    name: Annotated[str, typer.Argument(help="Team name.")],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Show a team."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        org_dto = resolve_org(environment, org)
        org_name = str(org_dto["name"])
        body = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.get(
                "/v1/teams", params={"orgId": org_dto["id"], "name": name}
            ),
        )
        items: list[dict[str, Any]] = body.get("items", [])
        if not items:
            raise store.TeamNotFoundError(
                f"No team {name!r} in organization {org!r}."
            )
        team = _cache_team(environment.name, org_name, items[0])
    render(_team_row(items[0], team), title=f"Team: {org_name}/{name}")


@app.command("list")
def list_(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    name: Annotated[
        str | None, typer.Option("--name", help="Filter by exact team name.")
    ] = None,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """List an organization's teams."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        org_dto = resolve_org(environment, org)
        org_name = str(org_dto["name"])
        params: dict[str, Any] = {"orgId": org_dto["id"]}
        if name is not None:
            params["name"] = name
        body = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.get("/v1/teams", params=params),
        )
        items: list[dict[str, Any]] = body.get("items", [])
        for item in items:
            _cache_team(environment.name, org_name, item)

    render(
        [
            {
                "name": item["name"],
                "description": item.get("description") or "",
                "server_team_id": item["id"],
            }
            for item in items
        ],
        title=f"Teams in {org_name}",
    )


@app.command("update")
def update(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    name: Annotated[str, typer.Argument(help="Current team name.")],
    new_name: Annotated[
        str | None, typer.Option("--name", help="New team name.")
    ] = None,
    description: Annotated[
        str | None, typer.Option("--description", help="New description.")
    ] = None,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Update a team's name and/or description."""
    if new_name is None and description is None:
        console.print(
            "[red]Error:[/red] nothing to update — supply --name and/or --description."
        )
        raise typer.Exit(1)
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        team_id, org_name = _resolve_team(caller, environment, org, name)
        body: dict[str, Any] = {}
        if new_name is not None:
            body["name"] = new_name
        if description is not None:
            body["description"] = description
        dto = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.patch(f"/v1/teams/{team_id}", json=body),
        )
        if new_name is not None and new_name != name:
            # the cache is keyed on (env, org, name); drop the stale row (cascades
            # its best-effort team_member rows) and re-cache under the new name.
            if store.get_team_or_none(environment.name, org_name, name) is not None:
                store.delete_team(environment.name, org_name, name)
        team = _cache_team(environment.name, org_name, dto)
    render(_team_row(dto, team), title="Team updated")


@app.command("delete")
def delete(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    name: Annotated[str, typer.Argument(help="Team name.")],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Delete a team (drops the local cache row and its team_member rows)."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        team_id, org_name = _resolve_team(caller, environment, org, name)
        auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.delete(f"/v1/teams/{team_id}"),
        )
        if store.get_team_or_none(environment.name, org_name, name) is not None:
            store.delete_team(environment.name, org_name, name)
    console.print(f"Team [bold]{org_name}/{name}[/bold] deleted.")


# --------------------------------------------------------------------------- #
# members
# --------------------------------------------------------------------------- #


@members_app.command("list")
def members_list(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    team_name: Annotated[str, typer.Argument(help="Team name.")],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """List a team's members (bulk-refreshes the local team_member cache)."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        team_id, org_name = _resolve_team(caller, environment, org, team_name)
        body = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.get(f"/v1/teams/{team_id}/members"),
        )
        rows: list[dict[str, Any]] = body.get("items", [])
        for row in rows:
            local = store.get_identity_by_server_id(
                environment.name, row["userId"], "USER"
            )
            if local is not None:
                store.upsert_team_member(
                    local.id, environment.name, org_name, team_name, row["role"]
                )

    render(
        [
            {
                "teamId": row["teamId"],
                "userId": row["userId"],
                "role": row["role"],
                "addedAt": row.get("addedAt", ""),
            }
            for row in rows
        ],
        title=f"Members of {org_name}/{team_name}",
    )


@members_app.command("add")
def members_add(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    team_name: Annotated[str, typer.Argument(help="Team name.")],
    user_id: Annotated[
        str, typer.Option("--user-id", help="Server-side user id to add.")
    ],
    role: Annotated[
        TeamRole | None,
        typer.Option("--role", help="Team role (default: TEAM_MEMBER)."),
    ] = None,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Add a member to a team."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        team_id, org_name = _resolve_team(caller, environment, org, team_name)
        payload: dict[str, Any] = {"userId": user_id}
        if role is not None:
            payload["role"] = role.value
        dto = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.post(f"/v1/teams/{team_id}/members", json=payload),
        )
        local = store.get_identity_by_server_id(environment.name, user_id, "USER")
        if local is not None:
            store.upsert_team_member(
                local.id, environment.name, org_name, team_name, dto["role"]
            )
    console.print(
        f"Added user [bold]{user_id}[/bold] to [bold]{org_name}/{team_name}[/bold] "
        f"as {dto['role']}."
    )


@members_app.command("set-role")
def members_set_role(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    team_name: Annotated[str, typer.Argument(help="Team name.")],
    user_id: Annotated[str, typer.Argument(help="Server-side user id.")],
    role: Annotated[TeamRole, typer.Option("--role", help="New team role.")],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Change an existing team member's role."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        team_id, org_name = _resolve_team(caller, environment, org, team_name)
        auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.patch(
                f"/v1/teams/{team_id}/members/{user_id}", json={"role": role.value}
            ),
        )
        local = store.get_identity_by_server_id(environment.name, user_id, "USER")
        if local is not None:
            store.upsert_team_member(
                local.id, environment.name, org_name, team_name, role.value
            )
    console.print(
        f"Set user [bold]{user_id}[/bold]'s role in "
        f"[bold]{org_name}/{team_name}[/bold] to {role.value}."
    )


@members_app.command("remove")
def members_remove(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    team_name: Annotated[str, typer.Argument(help="Team name.")],
    user_id: Annotated[str, typer.Argument(help="Server-side user id.")],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Remove a member from a team."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        team_id, org_name = _resolve_team(caller, environment, org, team_name)
        auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.delete(f"/v1/teams/{team_id}/members/{user_id}"),
        )
        local = store.get_identity_by_server_id(environment.name, user_id, "USER")
        if local is not None:
            store.delete_team_member(
                local.id, environment.name, org_name, team_name
            )
    console.print(
        f"Removed user [bold]{user_id}[/bold] from "
        f"[bold]{org_name}/{team_name}[/bold]."
    )


# --------------------------------------------------------------------------- #
# ingest clients
# --------------------------------------------------------------------------- #


@ingest_clients_app.command("list")
def ingest_clients_list(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    team_name: Annotated[str, typer.Argument(help="Team name.")],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """List a team's ingest clients (live view, no local write)."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        team_id, _ = _resolve_team(caller, environment, org, team_name)
        body = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.get(f"/v1/teams/{team_id}/ingest-clients"),
        )
        rows: list[dict[str, Any]] = body.get("items", [])

    render(
        [
            {
                "id": row["id"],
                "display_name": row.get("displayName", ""),
                "status": row.get("status", ""),
                "key_version": row.get("keyVersion", ""),
            }
            for row in rows
        ],
        title=f"Ingest clients of {org}/{team_name}",
    )


@ingest_clients_app.command("add")
def ingest_clients_add(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    team_name: Annotated[str, typer.Argument(help="Team name.")],
    display_name: Annotated[str, typer.Argument(help="Ingest-client display name.")],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Create a team ingest client — stored locally as a SERVICE_ACCOUNT identity."""
    label = slugify(f"{team_name}-{display_name}")
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        assert_label_free(environment.name, label)
        team_id, org_name = _resolve_team(caller, environment, org, team_name)
        dto = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.post(
                f"/v1/teams/{team_id}/ingest-clients",
                json={"displayName": display_name},
            ),
        )
        server_id = str(dto["id"])
        identity = store.add_identity(
            environment.name, "SERVICE_ACCOUNT", server_id, server_id, label
        )
        store.set_svc_acct(
            identity.id,
            environment.name,
            org_name,
            str(dto["clientSecret"]),
            key_version=dto.get("keyVersion"),
        )
    render(
        {
            "id": server_id,
            "display_name": dto.get("displayName", ""),
            "label": label,
            "key_version": dto.get("keyVersion", ""),
        },
        title="Ingest client created",
    )
    console.print(f"[bold]Client secret (shown once):[/bold] {dto['clientSecret']}")


@ingest_clients_app.command("rotate")
def ingest_clients_rotate(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    team_name: Annotated[str, typer.Argument(help="Team name.")],
    service_account_id: Annotated[
        str, typer.Argument(help="Server-side service-account id of the ingest client.")
    ],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Rotate an ingest client's client secret."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        team_id, _ = _resolve_team(caller, environment, org, team_name)
        dto = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.post(
                f"/v1/teams/{team_id}/ingest-clients/{service_account_id}/rotate"
            ),
        )
        local = store.get_identity_by_server_id(
            environment.name, service_account_id, "SERVICE_ACCOUNT"
        )
        if local is not None:
            store.update_svc_acct_secret(
                local.id, str(dto["clientSecret"]), dto.get("keyVersion")
            )
    console.print(
        f"Rotated ingest client [bold]{service_account_id}[/bold] "
        f"(key version {dto.get('keyVersion', '?')})."
    )
    console.print(f"[bold]New client secret (shown once):[/bold] {dto['clientSecret']}")


@ingest_clients_app.command("delete")
def ingest_clients_delete(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    team_name: Annotated[str, typer.Argument(help="Team name.")],
    service_account_id: Annotated[
        str, typer.Argument(help="Server-side service-account id of the ingest client.")
    ],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Delete a team ingest client."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        team_id, _ = _resolve_team(caller, environment, org, team_name)
        auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.delete(
                f"/v1/teams/{team_id}/ingest-clients/{service_account_id}"
            ),
        )
        local = store.get_identity_by_server_id(
            environment.name, service_account_id, "SERVICE_ACCOUNT"
        )
        if local is not None:
            store.delete_identity(environment.name, local.label)
            key_dir = store.keys_root() / service_account_id
            if key_dir.exists():  # ingest clients never have one; safe no-op
                shutil.rmtree(key_dir)
    console.print(
        f"Ingest client [bold]{service_account_id}[/bold] deleted."
    )
