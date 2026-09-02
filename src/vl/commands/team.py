"""`vl team` — manage VeritasLock teams.

Authenticates with Phase 3's `--as` / `VL_IDENTITY` / default identity + cached
token flow throughout. See vl-team-spec.md.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from vl.commands._shared import (
    AsOption,
    EnvOption,
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
    help="Manage VeritasLock teams.", no_args_is_help=True, cls=HelpOnErrorGroup
)


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
    name: Annotated[
        str | None, typer.Option("--name", help="Filter by exact team name.")
    ] = None,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """List teams.

    A PLATFORM_ADMIN caller sees every organization's teams; an ORG_ADMIN or USER
    sees the teams of the organization(s) they belong to. Org membership and role
    are read from the caller's token (the server's own authoritative view).
    """
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        orgs_claim = caller_orgs_claim(caller, environment)
        platform_admin = any(
            e.get("role") == "PLATFORM_ADMIN" for e in orgs_claim
        )

        if platform_admin:
            targets = [
                (str(o["id"]), str(o["name"])) for o in fetch_all_orgs(environment)
            ]
        else:
            resolve_name = org_name_resolver(environment)
            targets = [
                (str(e["orgId"]), resolve_name(str(e["orgId"])))
                for e in orgs_claim
            ]

        rows: list[dict[str, Any]] = []
        for org_id, org_name in targets:
            params: dict[str, Any] = {"orgId": org_id}
            if name is not None:
                params["name"] = name
            body = auth.authed_call(
                caller,
                environment.idp_base_url,
                lambda c: c.get("/v1/teams", params=params),
            )
            for item in body.get("items", []):
                _cache_team(environment.name, org_name, item)
                rows.append(
                    {
                        "org": org_name,
                        "name": item["name"],
                        "description": item.get("description") or "",
                        "server_team_id": item["id"],
                    }
                )

    render(
        rows,
        title="Teams (all organizations)" if platform_admin else "Teams",
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
