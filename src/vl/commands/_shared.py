"""Helpers shared across the resource-command modules."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
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


def cache_team(
    environment_name: str, org_name: str, dto: dict[str, Any]
) -> store.Team:
    """Upsert the local ``team`` cache row from a TeamDto; return it."""
    return store.upsert_team(
        environment_name,
        org_name,
        str(dto["name"]),
        str(dto["id"]),
        description=dto.get("description"),
        created_by=dto.get("createdBy"),
        created_at=str(dto["createdAt"]) if dto.get("createdAt") else None,
    )


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


def org_name_resolver(environment: store.Environment) -> Callable[[str], str]:
    """A memoised ``org id -> org name`` lookup for rendering server rows.

    Tries the local ``organization`` cache first, then a public
    ``GET /v1/organizations/{id}``, and finally falls back to the id itself so a
    listing never fails just because one org can't be resolved.
    """
    names: dict[str, str] = {
        org.server_org_id: org.name
        for org in store.list_organizations(environment.name)
    }

    def resolve(org_id: str) -> str:
        if not org_id:
            return "-"
        if org_id not in names:
            dto = fetch_org_by_id(environment.idp_base_url, org_id)
            names[org_id] = str(dto["name"]) if dto else org_id
        return names[org_id]

    return resolve


def fetch_all_orgs(environment: store.Environment) -> list[dict[str, Any]]:
    """Every organization, via the public paginated ``GET /v1/organizations``.

    Each page is upserted into the local ``organization`` cache as it's read.
    """
    orgs: list[dict[str, Any]] = []
    page = 0
    with api.IdpClient(environment.idp_base_url) as client:
        while True:
            body: dict[str, Any] = client.get(
                "/v1/organizations", params={"page": page}
            )
            items: list[dict[str, Any]] = body.get("items", [])
            orgs.extend(items)
            for item in items:
                cache_org(environment.name, item)
            cursor = body.get("nextCursor")
            if not cursor:
                return orgs
            page = int(cursor) if str(cursor).isdigit() else page + 1


def caller_orgs_claim(
    caller: store.Identity, environment: store.Environment
) -> list[dict[str, Any]]:
    """The caller token's ``orgs`` claim (``[{orgId, role}, ...]``).

    This is what the server bakes into the token at login and what its own
    org-standing checks read, so it's the authoritative view of which orgs the
    caller belongs to and with what role. Empty for a token that carries no
    ``orgs`` claim (e.g. a service account).
    """
    token = auth.get_token(caller, environment.idp_base_url)
    claims = api.decode_jwt_payload(token)
    orgs = claims.get("orgs")
    if not isinstance(orgs, list):
        return []
    return [e for e in orgs if isinstance(e, dict) and e.get("orgId")]


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
