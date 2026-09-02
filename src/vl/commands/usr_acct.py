"""`vl usr-acct` — manage VeritasLock user accounts.

Covers the whole lifecycle of a user as far as `vl` is concerned: create one
server-side (`add`) or cache an existing one (`cache` / `login`), pick it as the
identity commands run as (`use`), inspect (`show` / `list`), change (`update`),
and remove (`delete` server-side, `clear` locally).

Users are addressed locally by their **username** — there's no separate label.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any

import typer

from vl.commands._shared import (
    AsOption,
    EnvOption,
    cache_org,
    fetch_org_by_id,
    refuse_duplicate_account,
    report_errors,
    resolve_membership_org,
)
from vl.lib import api, auth, passwords, store
from vl.lib.output import console, render
from vl.lib.roles import UserOrgRole

app = typer.Typer(help="Manage VeritasLock user accounts.", no_args_is_help=True)


class UserStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


def _require_user_caller(identity: store.Identity) -> None:
    if identity.kind != "USER":
        raise auth.AuthError(
            f"identity '{identity.label}' is a {identity.kind} — only a USER "
            f"identity can manage users."
        )


def _local_user(environment_name: str, username: str) -> store.Identity:
    identity = store.get_identity(environment_name, username)
    if identity.kind != "USER":
        raise store.IdentityNotFoundError(
            f"{username!r} is a {identity.kind} identity, not a user."
        )
    return identity


def _secret_cell(cred: store.UserAcct | None, reveal: bool) -> str:
    if cred is None:
        return "(not cached)"
    if cred.password_plaintext is None:
        return "(not stored — token only)"
    return cred.password_plaintext if reveal else "********"


def _orgs_summary(identity_id: int) -> str:
    memberships = store.list_org_memberships(identity_id)
    return ", ".join(f"{m.org_name}:{m.role}" for m in memberships) or "-"


def _orgs_claim(claims: dict[str, Any]) -> list[dict[str, Any]]:
    orgs = claims.get("orgs")
    if not isinstance(orgs, list):
        return []
    return [
        e for e in orgs if isinstance(e, dict) and e.get("orgId") and e.get("role")
    ]


UsernameArg = Annotated[str, typer.Argument(help="Server-side username.")]


# --------------------------------------------------------------------------- #
# credential / identity management (was `vl identity`)
# --------------------------------------------------------------------------- #


@app.command("cache")
def cache(
    username: Annotated[str, typer.Option("--username", help="Server-side username.")],
    password: Annotated[
        str | None,
        typer.Option(
            "--password",
            help="Password (prompted if omitted). Passing it here exposes it in "
            "shell history / process list — prefer the prompt.",
        ),
    ] = None,
    env: EnvOption = None,
) -> None:
    """Cache an existing user account locally, storing its password for reuse.

    Authenticates *as the account being cached* — `--username`/`--password` are
    that account's own credential, not a caller's. Org memberships are read from
    the login token.
    """
    with report_errors():
        environment = store.get_environment(env)
        if password is None:
            password = typer.prompt("Password", hide_input=True)

        result = api.login(environment.idp_base_url, username, password)
        claims = api.decode_jwt_payload(result.access_token)
        server_id = str(claims.get("sub", ""))
        refuse_duplicate_account(environment.name, server_id, "USER")

        identity = store.add_identity(
            environment.name, "USER", server_id, username, username
        )
        store.set_user_acct(identity.id, password)

        for entry in _orgs_claim(claims):
            org_dto = fetch_org_by_id(environment.idp_base_url, str(entry["orgId"]))
            if org_dto is None:
                console.print(f"[dim]note: could not cache org {entry['orgId']}[/dim]")
                continue
            org_name = cache_org(environment.name, org_dto)
            store.upsert_org_membership(
                identity.id, environment.name, org_name, str(entry["role"])
            )

    console.print(
        f"Cached [bold]{username}[/bold] (password stored). "
        f"Run `vl usr-acct use {username}` to make it the default."
    )


@app.command("login")
def login(
    username: UsernameArg,
    env: EnvOption = None,
) -> None:
    """Authenticate as yourself without storing the password — only the token is cached.

    When that token expires `vl` re-prompts. Use `cache` instead to store the
    password for silent re-authentication.
    """
    with report_errors():
        environment = store.get_environment(env)
        password = typer.prompt("Password", hide_input=True)
        result = api.login(environment.idp_base_url, username, password)
        server_id = str(api.decode_jwt_payload(result.access_token).get("sub", ""))

        existing = store.get_identity_by_principal(environment.name, username, "USER")
        if existing is not None:
            identity = existing  # reuse — user_acct is left untouched
        else:
            identity = store.add_identity(
                environment.name, "USER", server_id, username, username
            )
            store.set_user_acct(identity.id, None)  # token-only

        issued = datetime.now(timezone.utc)
        store.set_cached_token(
            identity.id,
            result.access_token,
            issued,
            issued + timedelta(seconds=result.expires_in or 0),
        )

    console.print(
        f"Authenticated as [bold]{username}[/bold]; token cached (password not stored)."
    )


@app.command("use")
def use(username: UsernameArg, env: EnvOption = None) -> None:
    """Make this the environment's default identity."""
    with report_errors():
        environment = store.get_environment(env)
        _local_user(environment.name, username)
        store.set_default_identity(environment.name, username)
    console.print(
        f"Default identity for [bold]{environment.name}[/bold] is now "
        f"[bold]{username}[/bold]."
    )


@app.command("clear")
def clear(username: UsernameArg, env: EnvOption = None) -> None:
    """Drop a cached user account locally — no server call.

    Removes the identity row and everything hanging off it (stored password,
    cached memberships, cached token). The server-side account is untouched; use
    `vl usr-acct delete` for that.
    """
    with report_errors():
        environment = store.get_environment(env)
        _local_user(environment.name, username)
        store.delete_identity(environment.name, username)
    console.print(
        f"Cleared cached user [bold]{username}[/bold]. The server account is untouched."
    )


# --------------------------------------------------------------------------- #
# server resource management
# --------------------------------------------------------------------------- #


@app.command("add")
def add(
    first: Annotated[str, typer.Argument(help="First name.")],
    last: Annotated[str, typer.Argument(help="Last name.")],
    role: Annotated[UserOrgRole, typer.Option("--role", help="Org role to grant.")],
    org: Annotated[
        str | None,
        typer.Option("--org", help="Organization (default: the acting identity's org)."),
    ] = None,
    email: Annotated[
        str | None,
        typer.Option(
            "--email",
            help="Email (default: first.last@example.com — a placeholder; always "
            "set a real one for an account that may become an org owner).",
        ),
    ] = None,
    phone: Annotated[
        str | None,
        typer.Option(
            "--phone",
            help="Phone number (E.164). Required before this account can be "
            "granted an org's owner role.",
        ),
    ] = None,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Create a user server-side, cache its generated credential, print the password once.

    The username (first-initial + last name) is the local handle.
    """
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        _require_user_caller(caller)
        org_name_arg = resolve_membership_org(caller, org)

        username = f"{first[:1]}{last}".lower()
        user_email = email or f"{first.lower()}.{last.lower()}@example.com"
        display_name = f"{first} {last}"
        password = passwords.generate_password()
        user_id = str(uuid.uuid4())

        def _call(client: api.IdpClient) -> tuple[dict[str, Any], dict[str, Any]]:
            org_dto: dict[str, Any] = client.get(
                "/v1/organizations", params={"name": org_name_arg}
            )
            body: dict[str, Any] = {
                "id": user_id,
                "username": username,
                "email": user_email,
                "displayName": display_name,
                "passwordHash": passwords.bcrypt_hash(password),
                "status": "ACTIVE",
                "mfaEnabled": False,
                "organizations": [
                    {
                        "orgId": org_dto["id"],
                        "orgName": org_dto["name"],
                        "role": role.value,
                    }
                ],
            }
            if phone is not None:
                body["phoneNumber"] = phone
            user_dto: dict[str, Any] = client.post("/v1/users", json=body)
            return org_dto, user_dto

        org_dto, user_dto = auth.authed_call(caller, environment.idp_base_url, _call)

        org_name = cache_org(environment.name, org_dto)
        identity = store.add_identity(
            environment.name, "USER", user_dto["id"], username, username
        )
        store.set_user_acct(identity.id, password)
        store.upsert_org_membership(
            identity.id, environment.name, org_name, role.value
        )

    render(
        {
            "username": user_dto["username"],
            "email": user_dto["email"],
            "phone": user_dto.get("phoneNumber") or "",
            "display_name": user_dto.get("displayName", ""),
            "server_id": user_dto["id"],
        },
        title="User created",
    )
    console.print(f"[bold]Password (shown once):[/bold] {password}")


@app.command("show")
def show(
    username: UsernameArg,
    remote: Annotated[
        bool, typer.Option("--remote", help="Show the server record instead of the local one.")
    ] = False,
    all_: Annotated[
        bool, typer.Option("--all", help="Show the server record merged with local metadata.")
    ] = False,
    reveal_secret: Annotated[
        bool, typer.Option("--reveal-secret", help="Show the stored password.")
    ] = False,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Show a cached user account. Local view by default; --remote / --all hit the server."""
    with report_errors():
        environment = store.get_environment(env)
        identity = _local_user(environment.name, username)
        cred = store.get_user_acct(identity.id)

        server: dict[str, Any] | None = None
        if remote or all_:
            caller = store.resolve_identity(environment.name, as_)
            server = auth.authed_call(
                caller,
                environment.idp_base_url,
                lambda c: c.get(f"/v1/users/{identity.server_id}"),
            )

    if remote and not all_:
        assert server is not None
        render(_server_user_row(server), title=f"User (server): {username}")
        return

    row: dict[str, Any] = {
        "username": identity.principal_name,
        "server_id": identity.server_id,
        "default": "yes" if identity.is_default else "no",
        "password": _secret_cell(cred, reveal_secret),
        "orgs": _orgs_summary(identity.id),
    }
    if server is not None:
        row.update(_server_user_row(server))
    render(row, title=f"User: {username}")


def _server_user_row(dto: dict[str, Any]) -> dict[str, Any]:
    return {
        "username": dto["username"],
        "email": dto["email"],
        "phone": dto.get("phoneNumber") or "",
        "display_name": dto.get("displayName", ""),
        "status": dto["status"],
        "mfa_enabled": dto.get("mfaEnabled", False),
        "server_id": dto["id"],
    }


@app.command("list")
def list_(
    remote: Annotated[
        bool, typer.Option("--remote", help="List server-side users instead of cached ones.")
    ] = False,
    all_: Annotated[
        bool, typer.Option("--all", help="List server-side users, marking which are cached.")
    ] = False,
    status: Annotated[
        UserStatus | None,
        typer.Option("--status", help="Server filter (with --remote / --all)."),
    ] = None,
    email: Annotated[
        str | None,
        typer.Option("--email", help="Server filter (with --remote / --all)."),
    ] = None,
    page: Annotated[
        int, typer.Option("--page", min=0, help="Page (with --remote / --all).")
    ] = 0,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """List user accounts. Cached (local) by default; --remote / --all hit the server."""
    with report_errors():
        environment = store.get_environment(env)

        if not remote and not all_:
            rows = [
                {
                    "username": f"{i.principal_name} *"
                    if i.is_default
                    else i.principal_name,
                    "password": _secret_cell(store.get_user_acct(i.id), False),
                    "orgs": _orgs_summary(i.id),
                }
                for i in store.list_identities(environment.name, kind="USER")
            ]
            render(rows, title=f"Cached users ({environment.name})")
            return

        caller = store.resolve_identity(environment.name, as_)
        params: dict[str, Any] = {"page": page}
        if status is not None:
            params["status"] = status.value
        if email is not None:
            params["email"] = email
        body = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.get("/v1/users", params=params),
        )
        items: list[dict[str, Any]] = body.get("items", [])
        next_cursor = body.get("nextCursor")
        cached_ids = {
            i.server_id
            for i in store.list_identities(environment.name, kind="USER")
        }

    render(
        [
            {
                "username": item["username"],
                "email": item["email"],
                "status": item["status"],
                "server_id": item["id"],
                "cached": "yes" if item["id"] in cached_ids else "",
            }
            for item in items
        ],
        title="Users (server)",
    )
    if next_cursor:
        console.print(f"[dim]more results — rerun with --page {next_cursor}[/dim]")


@app.command("update")
def update(
    username: UsernameArg,
    display_name: Annotated[
        str | None, typer.Option("--display-name", help="New display name.")
    ] = None,
    status: Annotated[
        UserStatus | None, typer.Option("--status", help="New status.")
    ] = None,
    email: Annotated[str | None, typer.Option("--email", help="New email.")] = None,
    phone: Annotated[
        str | None, typer.Option("--phone", help="New phone number (E.164).")
    ] = None,
    mfa: Annotated[
        bool | None, typer.Option("--mfa/--no-mfa", help="Toggle MFA.")
    ] = None,
    password: Annotated[
        str | None, typer.Option("--password", help="Set a specific new password.")
    ] = None,
    rotate_password: Annotated[
        bool,
        typer.Option("--rotate-password", help="Generate and set a new random password."),
    ] = False,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Update a user (partial). A password change stays in sync with the local copy."""
    if password is not None and rotate_password:
        console.print(
            "[red]Error:[/red] pass either --password or --rotate-password, not both."
        )
        raise typer.Exit(1)

    new_password = password
    if rotate_password:
        new_password = passwords.generate_password()

    body: dict[str, Any] = {}
    if display_name is not None:
        body["displayName"] = display_name
    if status is not None:
        body["status"] = status.value
    if email is not None:
        body["email"] = email
    if phone is not None:
        body["phoneNumber"] = phone
    if mfa is not None:
        body["mfaEnabled"] = mfa
    if new_password is not None:
        body["passwordHash"] = passwords.bcrypt_hash(new_password)

    if not body:
        console.print(
            "[red]Error:[/red] nothing to update — supply at least one of "
            "--display-name / --status / --email / --phone / --mfa / --password / "
            "--rotate-password."
        )
        raise typer.Exit(1)

    with report_errors():
        environment = store.get_environment(env)
        identity = _local_user(environment.name, username)
        caller = store.resolve_identity(environment.name, as_)
        user_dto = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.patch(f"/v1/users/{identity.server_id}", json=body),
        )
        if new_password is not None:
            cred = store.get_user_acct(identity.id)
            if cred is not None and cred.password_plaintext is not None:
                store.set_user_acct(identity.id, new_password)

    render(_server_user_row(user_dto), title="User updated")
    if rotate_password:
        console.print(f"[bold]New password (shown once):[/bold] {new_password}")


@app.command("delete")
def delete(
    username: UsernameArg,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Delete a user server-side, then drop its local cache."""
    with report_errors():
        environment = store.get_environment(env)
        identity = _local_user(environment.name, username)
        caller = store.resolve_identity(environment.name, as_)
        auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.delete(f"/v1/users/{identity.server_id}"),
        )
        store.delete_identity(environment.name, username)
    console.print(f"User [bold]{username}[/bold] deleted.")
