"""`vl user` — manage VeritasLock users.

Every subcommand resolves the calling identity (`--as <label>` / `VL_IDENTITY` /
the environment default) and authenticates via the cached-token flow — there is no
unauthenticated user endpoint. See vl-identity-user-spec.md §9.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum
from typing import Annotated, Any

import typer

from vl.lib import api, auth, passwords, store
from vl.lib.output import console, render
from vl.lib.roles import UserOrgRole

app = typer.Typer(help="Manage VeritasLock users.", no_args_is_help=True)

EnvOption = Annotated[
    str | None,
    typer.Option("--env", help="Environment to target (default: the store's default)."),
]
AsOption = Annotated[
    str | None,
    typer.Option("--as", help="Identity label to run this command as."),
]


class UserStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


@contextmanager
def _report_errors() -> Iterator[None]:
    try:
        yield
    except (store.StoreError, api.ApiError, auth.AuthError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc


def _require_user_caller(identity: store.Identity) -> None:
    if identity.kind != "USER":
        raise auth.AuthError(
            f"identity '{identity.label}' is a {identity.kind} — only a USER "
            f"identity can manage users."
        )


@app.command("add")
def add(
    first: Annotated[str, typer.Argument(help="First name.")],
    last: Annotated[str, typer.Argument(help="Last name.")],
    org: Annotated[str, typer.Option("--org", help="Organization name for the membership.")],
    role: Annotated[UserOrgRole, typer.Option("--role", help="Org role to grant.")],
    label: Annotated[
        str | None,
        typer.Option("--label", help="Local identity label (default: derived username)."),
    ] = None,
    email: Annotated[
        str | None,
        typer.Option(
            "--email",
            help="Email address (default: first.last@example.com — a placeholder; "
            "always set a real one for an account that may become an org owner).",
        ),
    ] = None,
    phone: Annotated[
        str | None,
        typer.Option(
            "--phone",
            help="Phone number (E.164, e.g. +15555550123). Required before this "
            "account can be granted an org's owner role.",
        ),
    ] = None,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Create a user, store its generated credential, and print the password once."""
    with _report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        _require_user_caller(caller)

        username = f"{first[:1]}{last}".lower()
        user_email = email or f"{first.lower()}.{last.lower()}@example.com"
        display_name = f"{first} {last}"
        password = passwords.generate_password()
        user_id = str(uuid.uuid4())
        identity_label = label or username

        def _call(client: api.IdpClient) -> tuple[dict[str, Any], dict[str, Any]]:
            org_dto: dict[str, Any] = client.get(
                "/v1/organizations", params={"name": org}
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

        store.upsert_organization(
            environment.name,
            org_dto["name"],
            org_dto["id"],
            org_dto["displayName"],
            bool(org_dto["active"]),
        )
        identity = store.add_identity(
            environment.name, "USER", user_dto["id"], username, identity_label
        )
        store.set_user_credential(identity.id, password)
        store.upsert_org_membership(
            identity.id, environment.name, str(org_dto["name"]), role.value
        )

    render(
        {
            "username": user_dto["username"],
            "email": user_dto["email"],
            "phone": user_dto.get("phoneNumber") or "",
            "display_name": user_dto.get("displayName", ""),
            "server_id": user_dto["id"],
            "label": identity_label,
        },
        title="User created",
    )
    console.print(f"[bold]Password (shown once):[/bold] {password}")


@app.command("show")
def show(
    label: Annotated[str, typer.Argument(help="Local identity label of the user.")],
    reveal_secret: Annotated[
        bool, typer.Option("--reveal-secret", help="Show the stored password.")
    ] = False,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Show a user, merging the server record with local metadata."""
    with _report_errors():
        environment = store.get_environment(env)
        identity = store.get_identity(environment.name, label)
        caller = store.resolve_identity(environment.name, as_)
        user_dto = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.get(f"/v1/users/{identity.server_id}"),
        )
        cred = store.get_user_credential(identity.id)
        memberships = store.list_org_memberships(identity.id)

    if cred is None or cred.password_plaintext is None:
        secret = "(not stored)"
    else:
        secret = cred.password_plaintext if reveal_secret else "********"

    render(
        {
            "label": identity.label,
            "username": user_dto["username"],
            "email": user_dto["email"],
            "phone": user_dto.get("phoneNumber") or "",
            "display_name": user_dto.get("displayName", ""),
            "status": user_dto["status"],
            "mfa_enabled": user_dto.get("mfaEnabled", False),
            "server_id": user_dto["id"],
            "password": secret,
            "orgs": ", ".join(f"{m.org_name}:{m.role}" for m in memberships) or "-",
        },
        title=f"User: {label}",
    )


@app.command("list")
def list_(
    status: Annotated[
        UserStatus | None, typer.Option("--status", help="Filter by status.")
    ] = None,
    email: Annotated[
        str | None, typer.Option("--email", help="Filter by exact email.")
    ] = None,
    page: Annotated[int, typer.Option("--page", min=0, help="Page number (0-based).")] = 0,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """List server-side users (not local store rows)."""
    with _report_errors():
        environment = store.get_environment(env)
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

    render(
        [
            {
                "username": item["username"],
                "email": item["email"],
                "display_name": item.get("displayName", ""),
                "status": item["status"],
                "server_id": item["id"],
            }
            for item in items
        ],
        title="Users",
    )
    if next_cursor:
        console.print(f"[dim]more results — rerun with --page {next_cursor}[/dim]")


@app.command("update")
def update(
    label: Annotated[str, typer.Argument(help="Local identity label of the user.")],
    display_name: Annotated[
        str | None, typer.Option("--display-name", help="New display name.")
    ] = None,
    status: Annotated[
        UserStatus | None, typer.Option("--status", help="New status.")
    ] = None,
    email: Annotated[str | None, typer.Option("--email", help="New email.")] = None,
    phone: Annotated[
        str | None,
        typer.Option("--phone", help="New phone number (E.164, e.g. +15555550123)."),
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
    """Update a user (partial). Password change is tier-aware locally."""
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

    with _report_errors():
        environment = store.get_environment(env)
        identity = store.get_identity(environment.name, label)
        caller = store.resolve_identity(environment.name, as_)
        user_dto = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.patch(f"/v1/users/{identity.server_id}", json=body),
        )
        if new_password is not None:
            cred = store.get_user_credential(identity.id)
            if cred is not None and cred.password_plaintext is not None:
                store.set_user_credential(identity.id, new_password)  # tier 1: keep in sync

    render(
        {
            "label": identity.label,
            "username": user_dto["username"],
            "email": user_dto["email"],
            "phone": user_dto.get("phoneNumber") or "",
            "display_name": user_dto.get("displayName", ""),
            "status": user_dto["status"],
            "mfa_enabled": user_dto.get("mfaEnabled", False),
        },
        title="User updated",
    )
    if rotate_password:
        console.print(f"[bold]New password (shown once):[/bold] {new_password}")


@app.command("delete")
def delete(
    label: Annotated[str, typer.Argument(help="Local identity label of the user.")],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Delete a user server-side and drop its local identity."""
    with _report_errors():
        environment = store.get_environment(env)
        identity = store.get_identity(environment.name, label)
        caller = store.resolve_identity(environment.name, as_)
        auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.delete(f"/v1/users/{identity.server_id}"),
        )
        store.delete_identity(environment.name, label)
    console.print(f"User [bold]{label}[/bold] deleted.")
