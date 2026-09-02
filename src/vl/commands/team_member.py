"""`vl team-member` — list and add a team's members. Peer to `vl team`.

A team is addressed by **name alone**; `vl` resolves it across the
organization(s) the caller belongs to (every org for a PLATFORM_ADMIN), using
`--org` to disambiguate when more than one matches. A user is addressed by
**username** and must already belong to the team's organization — the same rule
the server enforces (`TeamService.addMember`), pre-checked here for a clean error.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from vl.commands._shared import (
    AsOption,
    CliError,
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
from vl.lib.roles import TeamRole

app = typer.Typer(
    help="List and add team members.",
    no_args_is_help=True,
    cls=HelpOnErrorGroup,
)

TeamArg = Annotated[str, typer.Argument(help="Team name.")]
OrgOption = Annotated[
    str | None,
    typer.Option(
        "--org",
        help="Organization the team is in — only needed to disambiguate when "
        "more than one of your orgs has a team by this name.",
    ),
]


def _resolve_team_by_name(
    caller: store.Identity,
    environment: store.Environment,
    name: str,
    org: str | None,
) -> tuple[str, str, str]:
    """``(server_team_id, org_id, org_name)`` for a team addressed by name alone.

    Searches ``GET /v1/teams?orgId=&name=`` across the caller's orgs (every org
    for a PLATFORM_ADMIN; just ``--org`` when given). Errors if zero or several
    orgs have a team by that name.
    """
    if org is not None:
        org_dto = resolve_org(environment, org)
        candidates = [(str(org_dto["id"]), str(org_dto["name"]))]
    else:
        orgs_claim = caller_orgs_claim(caller, environment)
        if any(e.get("role") == "PLATFORM_ADMIN" for e in orgs_claim):
            candidates = [
                (str(o["id"]), str(o["name"])) for o in fetch_all_orgs(environment)
            ]
        else:
            resolve_name = org_name_resolver(environment)
            candidates = [
                (str(e["orgId"]), resolve_name(str(e["orgId"])))
                for e in orgs_claim
            ]

    matches: list[tuple[dict[str, Any], str, str]] = []
    for org_id, org_name in candidates:
        body = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.get("/v1/teams", params={"orgId": org_id, "name": name}),
        )
        matches.extend(
            (item, org_id, org_name) for item in body.get("items", [])
        )

    if not matches:
        where = f"organization {org!r}" if org else "your organization(s)"
        raise store.TeamNotFoundError(f"No team named {name!r} in {where}.")
    if len(matches) > 1:
        orgs = ", ".join(sorted(m[2] for m in matches))
        raise CliError(
            f"More than one team is named {name!r} (in {orgs}). "
            f"Pass --org to say which one."
        )

    item, org_id, org_name = matches[0]
    cache_team(environment.name, org_name, item)
    return str(item["id"]), org_id, org_name


def _fetch_org_users(
    caller: store.Identity, environment: store.Environment, org_id: str
) -> list[dict[str, Any]]:
    """Every user with a membership in ``org_id`` (``GET /v1/users?orgId=``, all pages)."""
    users: list[dict[str, Any]] = []
    page = 0
    while True:
        body = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.get(
                "/v1/users", params={"orgId": org_id, "page": page, "limit": 200}
            ),
        )
        users.extend(body.get("items", []))
        cursor = body.get("nextCursor")
        if not cursor:
            return users
        page = int(cursor) if str(cursor).isdigit() else page + 1


@app.command("list")
def list_(
    team: TeamArg,
    org: OrgOption = None,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """List a team's members."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        team_id, org_id, org_name = _resolve_team_by_name(
            caller, environment, team, org
        )
        body = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.get(f"/v1/teams/{team_id}/members"),
        )
        members: list[dict[str, Any]] = body.get("items", [])

        usernames = {
            u["id"]: u["username"]
            for u in _fetch_org_users(caller, environment, org_id)
        }
        for member in members:
            local = store.get_identity_by_server_id(
                environment.name, member["userId"], "USER"
            )
            if local is not None:
                usernames.setdefault(member["userId"], local.principal_name)
                store.upsert_team_member(
                    local.id, environment.name, org_name, team, member["role"]
                )

    render(
        [
            {
                "username": usernames.get(member["userId"], "-"),
                "user_id": member["userId"],
                "role": member["role"],
                "added_at": member.get("addedAt", ""),
            }
            for member in members
        ],
        title=f"Members of {org_name}/{team}",
    )


@app.command("add")
def add(
    team: TeamArg,
    username: Annotated[str, typer.Argument(help="Username of the user to add.")],
    role: Annotated[
        TeamRole | None,
        typer.Option("--role", help="Team role (default: TEAM_MEMBER)."),
    ] = None,
    org: OrgOption = None,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Add a user to a team.

    The user must already be a member of the team's organization.
    """
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        team_id, org_id, org_name = _resolve_team_by_name(
            caller, environment, team, org
        )

        user = next(
            (
                u
                for u in _fetch_org_users(caller, environment, org_id)
                if u.get("username") == username
            ),
            None,
        )
        if user is None:
            raise CliError(
                f"No user {username!r} in organization {org_name!r} — a user must "
                f"belong to the team's organization before joining the team."
            )

        payload: dict[str, Any] = {"userId": user["id"]}
        if role is not None:
            payload["role"] = role.value
        dto = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.post(f"/v1/teams/{team_id}/members", json=payload),
        )

        local = store.get_identity_by_principal(
            environment.name, username, "USER"
        )
        if local is not None:
            store.upsert_team_member(
                local.id, environment.name, org_name, team, dto["role"]
            )

    console.print(
        f"Added [bold]{username}[/bold] to [bold]{org_name}/{team}[/bold] "
        f"as {dto['role']}."
    )
