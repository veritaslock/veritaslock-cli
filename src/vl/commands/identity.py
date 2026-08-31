"""`vl identity` — manage the identities `vl` can authenticate as.

`import` adopts an existing server-side user into a reusable tier-1 identity
(password stored); `login` establishes a tier-2 identity (password used once for a
token, never written). See vl-identity-user-spec.md §7–8.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated

import typer

from vl.lib import api, store
from vl.lib.output import console, render
from vl.lib.roles import OrgRole

app = typer.Typer(
    help="Manage the identities vl authenticates as.", no_args_is_help=True
)

EnvOption = Annotated[
    str | None,
    typer.Option("--env", help="Environment to target (default: the store's default)."),
]


class IdentityKind(str, Enum):
    USER = "USER"
    SERVICE_ACCOUNT = "SERVICE_ACCOUNT"


@contextmanager
def _report_errors() -> Iterator[None]:
    try:
        yield
    except (store.StoreError, api.ApiError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc


def _secret_cell(cred: store.UserCredential | None, reveal: bool) -> str:
    if cred is None or cred.password_plaintext is None:
        return "(not stored)"
    return cred.password_plaintext if reveal else "********"


@app.command("list")
def list_(env: EnvOption = None) -> None:
    """List stored identities for an environment."""
    with _report_errors():
        environment = store.get_environment(env)
        identities = store.list_identities(environment.name)
        rows = []
        for identity in identities:
            cred = store.get_user_credential(identity.id)
            memberships = store.list_org_memberships(identity.id)
            rows.append(
                {
                    "label": f"{identity.label} *" if identity.is_default else identity.label,
                    "kind": identity.kind,
                    "principal": identity.principal_name,
                    "orgs": ", ".join(f"{m.org_name}:{m.role}" for m in memberships) or "-",
                    "password": "stored"
                    if cred is not None and cred.password_plaintext is not None
                    else "no",
                }
            )
    render(rows, title=f"Identities ({environment.name})")


@app.command("show")
def show(
    label: Annotated[str, typer.Argument(help="Identity label.")],
    env: EnvOption = None,
    reveal_secret: Annotated[
        bool, typer.Option("--reveal-secret", help="Show the stored password.")
    ] = False,
) -> None:
    """Show a stored identity and its cached org memberships."""
    with _report_errors():
        environment = store.get_environment(env)
        identity = store.get_identity(environment.name, label)
        cred = store.get_user_credential(identity.id)
        memberships = store.list_org_memberships(identity.id)

    render(
        {
            "label": identity.label,
            "kind": identity.kind,
            "principal_name": identity.principal_name,
            "server_id": identity.server_id,
            "environment": identity.environment_name,
            "is_default": "yes" if identity.is_default else "no",
            "password": _secret_cell(cred, reveal_secret),
            "created_at": identity.created_at,
        },
        title=f"Identity: {label}",
    )
    if memberships:
        render(
            [
                {"org": m.org_name, "role": m.role, "synced_at": m.synced_at}
                for m in memberships
            ],
            title="Org memberships",
        )


@app.command("use")
def use(
    label: Annotated[str, typer.Argument(help="Identity label.")],
    env: EnvOption = None,
) -> None:
    """Set the default identity for an environment."""
    with _report_errors():
        environment = store.get_environment(env)
        store.set_default_identity(environment.name, label)
    console.print(
        f"Default identity for [bold]{environment.name}[/bold] is now "
        f"[bold]{label}[/bold]."
    )


@app.command("import")
def import_(
    username: Annotated[str, typer.Option("--username", help="Server-side username.")],
    org: Annotated[str, typer.Option("--org", help="Organization name for the membership.")],
    role: Annotated[OrgRole, typer.Option("--role", help="Asserted org role (not verified).")],
    label: Annotated[str, typer.Option("--label", help="Local label for this identity.")],
    kind: Annotated[
        IdentityKind, typer.Option("--kind", help="Identity kind.")
    ] = IdentityKind.USER,
    password: Annotated[
        str | None,
        typer.Option(
            "--password",
            help=(
                "Password (prompted if omitted). Passing it here exposes it in "
                "shell history / process list — prefer the prompt."
            ),
        ),
    ] = None,
    env: EnvOption = None,
) -> None:
    """Adopt an existing server-side identity as a reusable tier-1 identity."""
    if kind is not IdentityKind.USER:
        console.print(
            "[red]Error:[/red] --kind SERVICE_ACCOUNT is not supported yet "
            "(arrives in Phase 4)."
        )
        raise typer.Exit(1)

    with _report_errors():
        environment = store.get_environment(env)
        if password is None:
            password = typer.prompt("Password", hide_input=True)

        # 1. Resolve the org (unauthenticated) and cache it.
        with api.IdpClient(environment.idp_base_url) as client:
            org_dto = client.get("/v1/organizations", params={"name": org})
        store.upsert_organization(
            environment.name,
            org_dto["name"],
            org_dto["id"],
            org_dto["displayName"],
            bool(org_dto["active"]),
        )

        # 3. Validate the credential before storing anything.
        result = api.login(environment.idp_base_url, username, password)
        server_id = str(api.decode_jwt_payload(result.access_token).get("sub", ""))

        # 4. Persist: identity + stored credential (tier 1) + asserted membership.
        identity = store.add_identity(
            environment.name, "USER", server_id, username, label
        )
        store.set_user_credential(identity.id, password)
        store.upsert_org_membership(
            identity.id, environment.name, str(org_dto["name"]), role.value
        )

    console.print(
        f"Imported [bold]{label}[/bold] (tier 1 — password stored). "
        f"Run `vl identity use {label}` to make it the default."
    )


@app.command("login")
def login(
    username: Annotated[str, typer.Argument(help="Server-side username.")],
    env: EnvOption = None,
) -> None:
    """Authenticate as yourself without storing the password (tier 2)."""
    with _report_errors():
        environment = store.get_environment(env)
        password = typer.prompt("Password", hide_input=True)
        result = api.login(environment.idp_base_url, username, password)
        server_id = str(api.decode_jwt_payload(result.access_token).get("sub", ""))

        existing = store.get_identity_by_principal(environment.name, username, "USER")
        if existing is not None:
            identity = existing  # reuse — user_credential is left untouched
        else:
            identity = store.add_identity(
                environment.name, "USER", server_id, username, username
            )
            store.set_user_credential(identity.id, None)  # tier 2

        issued = datetime.now(timezone.utc)
        store.set_cached_token(
            identity.id,
            result.access_token,
            issued,
            issued + timedelta(seconds=result.expires_in or 0),
        )

    console.print(
        f"Authenticated as [bold]{identity.label}[/bold]; token cached "
        f"(password not stored)."
    )
