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
from pathlib import Path
from typing import Annotated, Any

import typer

from vl.lib import api, store
from vl.lib.output import console, render

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


def _secret_cell(cred: store.UserAcct | None, reveal: bool) -> str:
    if cred is None or cred.password_plaintext is None:
        return "(not stored)"
    return cred.password_plaintext if reveal else "********"


def _sa_secret_cell(cred: store.SvcAcct | None, reveal: bool) -> str:
    if cred is None:
        return "(not stored)"
    return cred.client_secret_plaintext if reveal else "********"


@app.command("list")
def list_(env: EnvOption = None) -> None:
    """List stored identities for an environment."""
    with _report_errors():
        environment = store.get_environment(env)
        identities = store.list_identities(environment.name)
        rows = []
        for identity in identities:
            if identity.kind == "SERVICE_ACCOUNT":
                sa = store.get_svc_acct(identity.id)
                orgs = sa.org_name if sa is not None else "-"
                secret = "stored"
            else:
                cred = store.get_user_acct(identity.id)
                memberships = store.list_org_memberships(identity.id)
                orgs = ", ".join(f"{m.org_name}:{m.role}" for m in memberships) or "-"
                secret = (
                    "stored"
                    if cred is not None and cred.password_plaintext is not None
                    else "no"
                )
            rows.append(
                {
                    "label": f"{identity.label} *" if identity.is_default else identity.label,
                    "kind": identity.kind,
                    "principal": identity.principal_name,
                    "orgs": orgs,
                    "secret": secret,
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
        row: dict[str, object] = {
            "label": identity.label,
            "kind": identity.kind,
            "principal_name": identity.principal_name,
            "server_id": identity.server_id,
            "environment": identity.environment_name,
            "is_default": "yes" if identity.is_default else "no",
            "created_at": identity.created_at,
        }
        memberships: list[store.OrgMembership] = []
        if identity.kind == "SERVICE_ACCOUNT":
            sa = store.get_svc_acct(identity.id)
            row["org"] = sa.org_name if sa else "-"
            row["secret"] = _sa_secret_cell(sa, reveal_secret)
            row["private_key_path"] = (sa.private_key_path if sa else None) or "(none)"
            row["public_key_path"] = (sa.public_key_path if sa else None) or "(none)"
            row["key_version"] = (sa.key_version if sa else None) or "-"
        else:
            row["password"] = _secret_cell(
                store.get_user_acct(identity.id), reveal_secret
            )
            memberships = store.list_org_memberships(identity.id)

    render(row, title=f"Identity: {label}")
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


@app.command("forget")
def forget(
    label: Annotated[str, typer.Argument(help="Identity label.")],
    env: EnvOption = None,
) -> None:
    """Remove a stored identity locally — no server call.

    Drops the identity row and everything hanging off it (stored password or
    client secret, cached org/team memberships, cached token). The server-side
    account is untouched; use `vl user delete` / `vl service-account delete` for
    that. Re-add it later with `vl identity import`.
    """
    with _report_errors():
        environment = store.get_environment(env)
        identity = store.get_identity(environment.name, label)  # 404s clearly
        store.delete_identity(environment.name, label)
    console.print(
        f"Forgot {identity.kind} identity [bold]{label}[/bold] "
        f"(principal {identity.principal_name!r}). The server account is untouched."
    )


def _cache_org(environment_name: str, org_dto: dict[str, object]) -> str:
    store.upsert_organization(
        environment_name,
        str(org_dto["name"]),
        str(org_dto["id"]),
        str(org_dto["displayName"]),
        bool(org_dto["active"]),
    )
    return str(org_dto["name"])


def _fetch_org_by_id(base_url: str, org_id: str) -> dict[str, Any] | None:
    """`GET /v1/organizations/{id}` (public). ``None`` if it can't be resolved."""
    try:
        with api.IdpClient(base_url) as client:
            dto: dict[str, Any] = client.get(f"/v1/organizations/{org_id}")
        return dto
    except api.ApiError:
        return None


def _orgs_claim(claims: dict[str, Any]) -> list[dict[str, Any]]:
    orgs = claims.get("orgs")
    if not isinstance(orgs, list):
        return []
    return [
        e for e in orgs if isinstance(e, dict) and e.get("orgId") and e.get("role")
    ]


def _refuse_duplicate(environment_name: str, server_id: str, kind: str) -> None:
    existing = store.get_identity_by_server_id(environment_name, server_id, kind)  # type: ignore[arg-type]
    if existing is not None:
        raise store.IdentityExistsError(
            f"That {kind} account is already imported as {existing.label!r} in "
            f"environment {environment_name!r}. Use that label, or "
            f"`vl identity forget {existing.label}` first."
        )


@app.command("import")
def import_(
    label: Annotated[str, typer.Option("--label", help="Local label for this identity.")],
    kind: Annotated[
        IdentityKind, typer.Option("--kind", help="Identity kind.")
    ] = IdentityKind.USER,
    username: Annotated[
        str | None, typer.Option("--username", help="USER kind: server-side username.")
    ] = None,
    password: Annotated[
        str | None,
        typer.Option(
            "--password",
            help="USER kind: password (prompted if omitted). Passing it here "
            "exposes it in shell history / process list — prefer the prompt.",
        ),
    ] = None,
    client_id: Annotated[
        str | None,
        typer.Option("--client-id", help="SERVICE_ACCOUNT kind: the client id."),
    ] = None,
    secret: Annotated[
        str | None,
        typer.Option("--secret", help="SERVICE_ACCOUNT kind: the client secret."),
    ] = None,
    private_key_path: Annotated[
        Path | None,
        typer.Option(
            "--private-key-path", help="SERVICE_ACCOUNT kind: existing private key file."
        ),
    ] = None,
    public_key_path: Annotated[
        Path | None,
        typer.Option(
            "--public-key-path", help="SERVICE_ACCOUNT kind: existing public key file."
        ),
    ] = None,
    env: EnvOption = None,
) -> None:
    """Adopt an existing server-side identity into the local store.

    Authenticates *as the account being imported* — you need that account's own
    credential. Org memberships are read from the login response, not supplied.
    """
    if kind is IdentityKind.USER:
        _import_user(label, username, password, env)
    else:
        _import_service_account(
            label, client_id, secret, private_key_path, public_key_path, env
        )


def _import_user(
    label: str, username: str | None, password: str | None, env: str | None
) -> None:
    if username is None:
        raise typer.BadParameter("--username is required for --kind USER")

    with _report_errors():
        environment = store.get_environment(env)
        if password is None:
            password = typer.prompt("Password", hide_input=True)

        # Validate the credential (and resolve the account) before storing anything.
        result = api.login(environment.idp_base_url, username, password)
        claims = api.decode_jwt_payload(result.access_token)
        server_id = str(claims.get("sub", ""))
        _refuse_duplicate(environment.name, server_id, "USER")

        identity = store.add_identity(
            environment.name, "USER", server_id, username, label
        )
        store.set_user_acct(identity.id, password)

        # Real org memberships come from the token's `orgs` claim, not a flag.
        for entry in _orgs_claim(claims):
            org_dto = _fetch_org_by_id(
                environment.idp_base_url, str(entry["orgId"])
            )
            if org_dto is None:
                console.print(
                    f"[dim]note: could not cache org {entry['orgId']}[/dim]"
                )
                continue
            org_name = _cache_org(environment.name, org_dto)
            store.upsert_org_membership(
                identity.id, environment.name, org_name, str(entry["role"])
            )

    console.print(
        f"Imported [bold]{label}[/bold] (tier 1 — password stored). "
        f"Run `vl identity use {label}` to make it the default."
    )


def _import_service_account(
    label: str,
    client_id: str | None,
    secret: str | None,
    private_key_path: Path | None,
    public_key_path: Path | None,
    env: str | None,
) -> None:
    if client_id is None or secret is None:
        raise typer.BadParameter(
            "--client-id and --secret are required for --kind SERVICE_ACCOUNT"
        )

    with _report_errors():
        environment = store.get_environment(env)
        _refuse_duplicate(environment.name, client_id, "SERVICE_ACCOUNT")

        # Validate the secret before storing anything.
        token = api.service_account_token(
            environment.idp_base_url, client_id, secret
        ).access_token
        # Read the account's actual keyVersion and org — an adopted account may
        # already have been rotated server-side.
        with api.IdpClient(environment.idp_base_url, token=token) as client:
            sa_dto = client.get(f"/v1/service-accounts/{client_id}")

        org_dto = _fetch_org_by_id(environment.idp_base_url, str(sa_dto["orgId"]))
        if org_dto is None:
            raise store.OrganizationNotFoundError(
                f"Could not resolve the service account's organization "
                f"({sa_dto['orgId']})."
            )
        org_name = _cache_org(environment.name, org_dto)

        identity = store.add_identity(
            environment.name, "SERVICE_ACCOUNT", client_id, client_id, label
        )
        store.set_svc_acct(
            identity.id,
            environment.name,
            org_name,
            secret,
            public_key_path=str(public_key_path) if public_key_path else None,
            private_key_path=str(private_key_path) if private_key_path else None,
            key_version=sa_dto.get("keyVersion"),
        )

    console.print(
        f"Imported [bold]{label}[/bold] (SERVICE_ACCOUNT). "
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
            identity = existing  # reuse — user_acct is left untouched
        else:
            identity = store.add_identity(
                environment.name, "USER", server_id, username, username
            )
            store.set_user_acct(identity.id, None)  # tier 2

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
