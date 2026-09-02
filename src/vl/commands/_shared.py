"""Helpers shared across the resource-command modules."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any

import typer

from vl.lib import api, auth, store
from vl.lib.output import console

EnvOption = Annotated[
    str | None,
    typer.Option("--env", help="Environment to target (default: the store's default)."),
]
AsOption = Annotated[
    str | None,
    typer.Option(
        "--as",
        help="Identity label to act as (default: the environment's default identity).",
    ),
]


class CliError(Exception):
    """An expected command failure that should surface as one clean line."""


@contextmanager
def report_errors() -> Iterator[None]:
    """Render a store / API / auth / CLI error as one clean line + non-zero exit."""
    try:
        yield
    except (store.StoreError, auth.AuthError, CliError) as exc:
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


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def cache_org(environment_name: str, dto: dict[str, Any]) -> str:
    """Upsert the local `organization` cache row from an OrganizationDto; return its name."""
    store.upsert_organization(
        environment_name,
        dto["name"],
        dto["id"],
        dto["displayName"],
        bool(dto["active"]),
    )
    return str(dto["name"])


def resolve_org(environment: store.Environment, name: str) -> dict[str, Any]:
    """`GET /v1/organizations?name=` (public), cache it, return the DTO."""
    with api.IdpClient(environment.idp_base_url) as client:
        dto: dict[str, Any] = client.get("/v1/organizations", params={"name": name})
    cache_org(environment.name, dto)
    return dto


def fetch_org_by_id(base_url: str, org_id: str) -> dict[str, Any] | None:
    """`GET /v1/organizations/{id}` (public). ``None`` if it can't be resolved."""
    try:
        with api.IdpClient(base_url) as client:
            dto: dict[str, Any] = client.get(f"/v1/organizations/{org_id}")
        return dto
    except api.ApiError:
        return None


def resolve_membership_org(
    identity: store.Identity, explicit: str | None
) -> str:
    """Org name for a membership context: explicit ``--org`` wins, else the acting
    identity's single default org, else a clear error.
    """
    if explicit is not None:
        return explicit
    default = store.default_org_for_identity(identity)
    if default is None:
        raise CliError(
            f"no --org given and identity {identity.label!r} has no single default "
            f"org (see `vl whoami`) — pass --org explicitly."
        )
    return default


def assert_label_free(environment_name: str, label: str) -> None:
    """Raise ``IdentityExistsError`` if ``label`` is already taken in the environment."""
    try:
        store.get_identity(environment_name, label)
    except store.IdentityNotFoundError:
        return
    raise store.IdentityExistsError(
        f"Identity {label!r} already exists in environment {environment_name!r}. "
        f"Pick another --label."
    )


def refuse_duplicate_account(
    environment_name: str, server_id: str, kind: str
) -> None:
    """Raise if the same server account is already cached locally, naming its label."""
    existing = store.get_identity_by_server_id(environment_name, server_id, kind)  # type: ignore[arg-type]
    if existing is not None:
        verb = "usr-acct" if kind == "USER" else "svc-acct"
        raise store.IdentityExistsError(
            f"That {kind} account is already cached as {existing.label!r} in "
            f"environment {environment_name!r}. Run `vl {verb} clear "
            f"{existing.label}` first if you want to re-cache it."
        )
