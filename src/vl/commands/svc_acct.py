"""`vl svc-acct` — manage VeritasLock service accounts.

Replaces `provision_service_account.sh` / `deprovision_service_account.sh` /
`gen_assertion.py`. A service account provisioned via `add` gets both a symmetric
client secret and an Ed25519 keypair; one adopted via `cache` gets whatever the
server already has. See vl-service-account-spec.md.
"""

from __future__ import annotations

import secrets
import shutil
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import typer

from vl.commands._shared import (
    AsOption,
    CliError,
    EnvOption,
    assert_label_free,
    cache_org,
    fetch_org_by_id,
    org_name_resolver,
    refuse_duplicate_account,
    report_errors,
    resolve_membership_org,
    resolve_org,
    slugify,
)
from vl.lib import api, auth, keys, store
from vl.lib.cli import HelpOnErrorGroup
from vl.lib.output import console, note, render
from vl.lib.roles import ServiceAccountRole

app = typer.Typer(
    help="Manage VeritasLock service accounts.",
    no_args_is_help=True,
    cls=HelpOnErrorGroup,
)


class ServiceAccountStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DELETED = "DELETED"


class ServiceAccountError(CliError):
    """A `vl svc-acct` precondition failed (wrong kind, missing key, …)."""


def _local_sa(environment_name: str, label: str) -> store.Identity:
    identity = store.get_identity(environment_name, label)
    if identity.kind != "SERVICE_ACCOUNT":
        raise ServiceAccountError(
            f"{label!r} is a {identity.kind} identity, not a service account."
        )
    return identity


def _sa_secret_cell(cred: store.SvcAcct | None, reveal: bool) -> str:
    if cred is None:
        return "(not cached)"
    return cred.client_secret_plaintext if reveal else "********"


@app.command("add")
def add(
    display_name: Annotated[str, typer.Argument(help="Display name.")],
    role: Annotated[
        ServiceAccountRole, typer.Option("--role", help="Service-account role.")
    ],
    org: Annotated[
        str | None,
        typer.Option("--org", help="Organization (default: the acting identity's org)."),
    ] = None,
    secret: Annotated[
        str | None,
        typer.Option("--secret", help="Client secret (random hex if omitted)."),
    ] = None,
    description: Annotated[
        str | None, typer.Option("--description", help="Free-text description.")
    ] = None,
    label: Annotated[
        str | None,
        typer.Option("--label", help="Local label (default: slugified display name)."),
    ] = None,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Create a service account, provision its keypair, cache it locally."""
    identity_label = label or slugify(display_name)
    if not identity_label:
        raise typer.BadParameter(
            "could not derive a label from the display name — pass --label",
            param_hint="--label",
        )
    client_secret = secret or secrets.token_hex(16)

    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        assert_label_free(environment.name, identity_label)
        org_name_arg = resolve_membership_org(caller, org)

        with api.IdpClient(environment.idp_base_url) as client:
            org_dto = client.get("/v1/organizations", params={"name": org_name_arg})
        org_name = cache_org(environment.name, org_dto)

        created = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.post(
                "/v1/service-accounts",
                json={
                    "displayName": display_name,
                    "description": description,
                    "role": role.value,
                    "keyVersion": 1,
                    "clientSecret": client_secret,
                    "orgId": org_dto["id"],
                    "publicKey": None,
                    "bootstrapHash": None,
                },
            ),
        )
        server_id = str(created["id"])

        key_dir = store.keys_root() / server_id
        private_path, public_path = keys.generate_keypair(key_dir)
        _provision_public_key(
            environment.idp_base_url,
            server_id,
            str(created["bootstrapHash"]),
            keys.public_key_b64url(public_path),
        )

        identity = store.add_identity(
            environment.name, "SERVICE_ACCOUNT", server_id, server_id, identity_label
        )
        store.set_svc_acct(
            identity.id,
            environment.name,
            org_name,
            client_secret,
            public_key_path=str(public_path),
            private_key_path=str(private_path),
            key_version=1,
            role=role.value,
        )

    render(_sa_row(created, None), title="Service account created")
    console.print(f"[bold]Client secret (shown once):[/bold] {client_secret}")


def _provision_public_key(
    base_url: str, sa_id: str, bootstrap_hash: str, public_key_b64url: str
) -> None:
    """PATCH the first public key (bootstrapHash-gated, no token). Retry once."""
    last_error: api.ApiError | None = None
    for _ in range(2):
        try:
            with api.IdpClient(base_url) as client:
                client.patch(
                    f"/v1/service-accounts/{sa_id}/public-key",
                    params={"bootstrapHash": bootstrap_hash},
                    json={"publicKey": public_key_b64url},
                )
            return
        except api.ApiError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


@app.command("cache")
def cache(
    client_id: Annotated[str, typer.Option("--client-id", help="The client id.")],
    secret: Annotated[str, typer.Option("--secret", help="The client secret.")],
    label: Annotated[
        str | None,
        typer.Option("--label", help="Local label (default: slugified display name)."),
    ] = None,
    private_key_path: Annotated[
        Path | None,
        typer.Option(
            "--private-key-path",
            help="Existing Ed25519 private key to import (copied into vl's key store, "
            "with public.key derived from it). Only needed for `get-assertion`.",
        ),
    ] = None,
    env: EnvOption = None,
) -> None:
    """Cache an existing service account locally (org resolved from the server)."""
    with report_errors():
        environment = store.get_environment(env)
        refuse_duplicate_account(environment.name, client_id, "SERVICE_ACCOUNT")

        if private_key_path is not None and not private_key_path.is_file():
            raise ServiceAccountError(f"{private_key_path} is not a file.")

        token = api.service_account_token(
            environment.idp_base_url, client_id, secret
        ).access_token
        with api.IdpClient(environment.idp_base_url, token=token) as client:
            sa_dto = client.get(f"/v1/service-accounts/{client_id}")

        identity_label = label or slugify(str(sa_dto.get("displayName") or ""))
        if not identity_label:
            raise ServiceAccountError(
                "could not derive a label from the display name — pass --label"
            )
        assert_label_free(environment.name, identity_label)

        org_dto = fetch_org_by_id(environment.idp_base_url, str(sa_dto["orgId"]))
        if org_dto is None:
            raise store.OrganizationNotFoundError(
                f"Could not resolve the service account's organization "
                f"({sa_dto['orgId']})."
            )
        org_name = cache_org(environment.name, org_dto)

        stored_private: str | None = None
        stored_public: str | None = None
        if private_key_path is not None:
            try:
                priv, pub = keys.import_private_key(
                    private_key_path, store.keys_root() / client_id
                )
            except (OSError, ValueError) as exc:
                raise ServiceAccountError(str(exc)) from exc
            stored_private, stored_public = str(priv), str(pub)

        identity = store.add_identity(
            environment.name, "SERVICE_ACCOUNT", client_id, client_id, identity_label
        )
        store.set_svc_acct(
            identity.id,
            environment.name,
            org_name,
            secret,
            public_key_path=stored_public,
            private_key_path=stored_private,
            key_version=sa_dto.get("keyVersion"),
            role=sa_dto.get("role"),
        )

    console.print(
        f"Cached [bold]{identity_label}[/bold]. Run `vl svc-acct use "
        f"{identity_label}` to make it the default."
    )


@app.command("use")
def use(
    label: Annotated[str, typer.Argument(help="Local account label.")],
    env: EnvOption = None,
) -> None:
    """Make this the environment's default identity."""
    with report_errors():
        environment = store.get_environment(env)
        _local_sa(environment.name, label)
        store.set_default_identity(environment.name, label)
    console.print(
        f"Default identity for [bold]{environment.name}[/bold] is now [bold]{label}[/bold]."
    )


@app.command("clear")
def clear(
    label: Annotated[str, typer.Argument(help="Local account label.")],
    env: EnvOption = None,
) -> None:
    """Drop a cached service account locally — no server call.

    Removes the identity row, its secret, and its key files. The server-side
    account is untouched; use `vl svc-acct delete` for that.
    """
    with report_errors():
        environment = store.get_environment(env)
        identity = _local_sa(environment.name, label)
        store.delete_identity(environment.name, label)
        key_dir = store.keys_root() / identity.server_id
        if key_dir.exists():
            shutil.rmtree(key_dir)
    console.print(
        f"Cleared cached service account [bold]{label}[/bold]. The server account "
        f"is untouched."
    )


def _sa_row(dto: dict[str, Any], cred: store.SvcAcct | None) -> dict[str, Any]:
    return {
        "id": dto["id"],
        "display_name": dto.get("displayName", ""),
        "role": dto.get("role", ""),
        "status": dto.get("status", ""),
        "key_version": dto.get("keyVersion", ""),
        "keypair": "provisioned" if dto.get("publicKey") else "none",
        "private_key_path": (cred.private_key_path if cred else None) or "(none)",
    }


@app.command("show")
def show(
    label: Annotated[str, typer.Argument(help="Local account label.")],
    remote: Annotated[
        bool, typer.Option("--remote", help="Show the server record instead of the local one.")
    ] = False,
    all_: Annotated[
        bool, typer.Option("--all", help="Show the server record merged with local metadata.")
    ] = False,
    reveal_secret: Annotated[
        bool, typer.Option("--reveal-secret", help="Show the stored client secret.")
    ] = False,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Show a cached service account. Local view by default; --remote / --all hit the server."""
    with report_errors():
        environment = store.get_environment(env)
        identity = _local_sa(environment.name, label)
        cred = store.get_svc_acct(identity.id)

        server: dict[str, Any] | None = None
        if remote or all_:
            caller = store.resolve_identity(environment.name, as_)
            server = auth.authed_call(
                caller,
                environment.idp_base_url,
                lambda c: c.get(f"/v1/service-accounts/{identity.server_id}"),
            )
            if cred is not None:
                store.refresh_svc_acct_server_fields(
                    identity.id,
                    role=server.get("role"),
                    key_version=server.get("keyVersion"),
                )

    if remote and not all_:
        assert server is not None
        render(_server_sa_row(server), title=f"Service account (server): {label}")
        return

    row: dict[str, Any] = {
        "label": identity.label,
        "id": identity.server_id,
        "default": "yes" if identity.is_default else "no",
        "org": cred.org_name if cred else "-",
        "role": (cred.role if cred else None) or "-",
        "client_secret": _sa_secret_cell(cred, reveal_secret),
        "key_version": (cred.key_version if cred else None) or "-",
        "private_key_path": (cred.private_key_path if cred else None) or "(none)",
        "public_key_path": (cred.public_key_path if cred else None) or "(none)",
    }
    if server is not None:
        row.update(_server_sa_row(server))
    render(row, title=f"Service account: {label}")


def _server_sa_row(dto: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": dto["id"],
        "display_name": dto.get("displayName", ""),
        "description": dto.get("description") or "",
        "role": dto.get("role", ""),
        "status": dto.get("status", ""),
        "key_version": dto.get("keyVersion", ""),
    }


@app.command("list")
def list_(
    remote: Annotated[
        bool, typer.Option("--remote", help="List server-side accounts instead of cached ones.")
    ] = False,
    all_: Annotated[
        bool, typer.Option("--all", help="List server-side accounts, marking which are cached.")
    ] = False,
    org: Annotated[
        str | None,
        typer.Option(
            "--org",
            help="Filter to accounts in this org. Local by default; with "
            "--remote / --all it's sent as the server's orgId filter.",
        ),
    ] = None,
    role: Annotated[
        ServiceAccountRole | None,
        typer.Option("--role", help="Server filter (with --remote / --all)."),
    ] = None,
    status: Annotated[
        ServiceAccountStatus | None,
        typer.Option("--status", help="Server filter (with --remote / --all)."),
    ] = None,
    include_deleted: Annotated[
        bool, typer.Option("--include-deleted", help="Include DELETED (with --remote / --all).")
    ] = False,
    page: Annotated[
        int, typer.Option("--page", min=0, help="Page (with --remote / --all).")
    ] = 0,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """List service accounts. Cached (local) by default; --remote / --all hit the server."""
    with report_errors():
        environment = store.get_environment(env)

        if not remote and not all_:
            identities = store.list_identities(
                environment.name, kind="SERVICE_ACCOUNT"
            )
            if org is not None:
                identities = [i for i in identities if _sa_org(i.id) == org]
            rows = [
                {
                    "label": f"{i.label} *" if i.is_default else i.label,
                    "id": i.server_id,
                    "org": _sa_org(i.id),
                    "role": _sa_role(i.id),
                    "key_version": _sa_key_version(i.id),
                    "secret": "stored",
                }
                for i in identities
            ]
            title = f"Cached service accounts ({environment.name})"
            if org is not None:
                title += f" in {org}"
            render(rows, title=title)
            return

        caller = store.resolve_identity(environment.name, as_)
        params: dict[str, Any] = {"page": page}
        if role is not None:
            params["role"] = role.value
        if status is not None:
            params["status"] = status.value
        if include_deleted:
            params["includeDeleted"] = "true"
        if org is not None:
            params["orgId"] = resolve_org(environment, org)["id"]
        body = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.get("/v1/service-accounts", params=params),
        )
        items: list[dict[str, Any]] = body.get("items", [])
        next_cursor = body.get("nextCursor")
        by_server_id = {
            i.server_id: i
            for i in store.list_identities(environment.name, kind="SERVICE_ACCOUNT")
        }
        for item in items:
            local = by_server_id.get(item["id"])
            if local is not None:
                store.refresh_svc_acct_server_fields(
                    local.id,
                    role=item.get("role"),
                    key_version=item.get("keyVersion"),
                )
        resolve_org_name = org_name_resolver(environment)

    render(
        [
            {
                "id": item["id"],
                "display_name": item.get("displayName", ""),
                "org": resolve_org_name(str(item.get("orgId") or "")),
                "role": item.get("role", ""),
                "status": item.get("status", ""),
                "cached": "yes" if item["id"] in by_server_id else "no",
                "label": _local_label(by_server_id.get(item["id"])),
            }
            for item in items
        ],
        title="Service accounts (server)"
        + (f" in {org}" if org is not None else ""),
    )
    if next_cursor:
        note(f"more results — rerun with --page {next_cursor}")


def _sa_org(identity_id: int) -> str:
    cred = store.get_svc_acct(identity_id)
    return cred.org_name if cred else "-"


def _sa_key_version(identity_id: int) -> str:
    cred = store.get_svc_acct(identity_id)
    return str(cred.key_version) if cred and cred.key_version is not None else "-"


def _sa_role(identity_id: int) -> str:
    cred = store.get_svc_acct(identity_id)
    return (cred.role if cred else None) or "-"


def _local_label(identity: store.Identity | None) -> str:
    if identity is None:
        return ""
    return f"{identity.label} *" if identity.is_default else identity.label


@app.command("update")
def update(
    label: Annotated[str, typer.Argument(help="Local account label.")],
    display_name: Annotated[
        str | None, typer.Option("--display-name", help="New display name.")
    ] = None,
    description: Annotated[
        str | None, typer.Option("--description", help="New description.")
    ] = None,
    status: Annotated[
        ServiceAccountStatus | None, typer.Option("--status", help="New status.")
    ] = None,
    role: Annotated[
        ServiceAccountRole | None, typer.Option("--role", help="New role.")
    ] = None,
    json_metadata: Annotated[
        str | None, typer.Option("--json-metadata", help="New jsonMetadata blob.")
    ] = None,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Update a service account (partial). Does not touch key material."""
    body: dict[str, Any] = {}
    if display_name is not None:
        body["displayName"] = display_name
    if description is not None:
        body["description"] = description
    if status is not None:
        body["status"] = status.value
    if role is not None:
        body["role"] = role.value
    if json_metadata is not None:
        body["jsonMetadata"] = json_metadata
    if not body:
        console.print(
            "[red]Error:[/red] nothing to update — supply at least one of "
            "--display-name / --description / --status / --role / --json-metadata."
        )
        raise typer.Exit(1)

    with report_errors():
        environment = store.get_environment(env)
        identity = _local_sa(environment.name, label)
        caller = store.resolve_identity(environment.name, as_)
        dto = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.patch(
                f"/v1/service-accounts/{identity.server_id}", json=body
            ),
        )
        if role is not None:
            store.update_svc_acct_role(identity.id, role.value)

    render(
        _sa_row(dto, store.get_svc_acct(identity.id)), title="Service account updated"
    )


# NOTE: `rotate-keys` was removed. The IdP has no working re-key path for an
# already-active service account (the bootstrap hash is one-time and the key
# model is single-key), so the command could never succeed. It will come back
# once the server supports rotation — see
# docs/features/cli-implementation/svc-acct-key-rotation-gap.md.


@app.command("delete")
def delete(
    label: Annotated[str, typer.Argument(help="Local account label.")],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Soft-delete server-side, then drop the local cache and key files."""
    with report_errors():
        environment = store.get_environment(env)
        identity = _local_sa(environment.name, label)
        caller = store.resolve_identity(environment.name, as_)
        auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.delete(f"/v1/service-accounts/{identity.server_id}"),
        )
        store.delete_identity(environment.name, label)
        key_dir = store.keys_root() / identity.server_id
        if key_dir.exists():
            shutil.rmtree(key_dir)
    console.print(f"Service account [bold]{label}[/bold] deleted.")


@app.command("get-assertion")
def get_assertion(
    label: Annotated[str, typer.Argument(help="Local account label.")],
    aud: Annotated[
        str | None,
        typer.Option("--aud", help="Audience URL (default: the env's SA authenticate endpoint)."),
    ] = None,
    env: EnvOption = None,
) -> None:
    """Print a signed short-lived EdDSA JWT assertion for this service account."""
    with report_errors():
        environment = store.get_environment(env)
        identity = _local_sa(environment.name, label)
        cred = store.get_svc_acct(identity.id)
        if cred is None or cred.private_key_path is None:
            raise ServiceAccountError(
                f"no private key path recorded for {label!r} — cache it with "
                f"--private-key-path, or provision a fresh account with "
                f"`vl svc-acct add`."
            )
        audience = aud or (
            environment.idp_base_url.rstrip("/") + "/auth/service-account/authenticate"
        )
        try:
            token = keys.sign_assertion(
                Path(cred.private_key_path),
                subject=identity.server_id,
                audience=audience,
            )
        except (OSError, ValueError) as exc:
            raise ServiceAccountError(
                f"could not read the private key at {cred.private_key_path}: {exc}"
            ) from exc
    print(token)
