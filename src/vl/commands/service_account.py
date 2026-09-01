"""`vl service-account` — manage VeritasLock service accounts.

Replaces `provision_service_account.sh` / `deprovision_service_account.sh` /
`gen_assertion.py`. Every service account gets both a symmetric client secret
(for `/auth/service-account/token`) and an Ed25519 keypair (for signed JWT
assertions). See vl-service-account-spec.md.
"""

from __future__ import annotations

import re
import secrets
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import typer

from vl.lib import api, auth, keys, store
from vl.lib.output import console, render
from vl.lib.roles import ServiceAccountRole

app = typer.Typer(help="Manage VeritasLock service accounts.", no_args_is_help=True)

EnvOption = Annotated[
    str | None,
    typer.Option("--env", help="Environment to target (default: the store's default)."),
]
AsOption = Annotated[
    str | None, typer.Option("--as", help="Identity label to run this command as.")
]


class ServiceAccountStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DELETED = "DELETED"


class ServiceAccountError(Exception):
    """A `vl service-account` precondition failed (wrong kind, missing key, …)."""


@contextmanager
def _report_errors() -> Iterator[None]:
    try:
        yield
    except (store.StoreError, api.ApiError, auth.AuthError, ServiceAccountError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc
    except (OSError, ValueError) as exc:  # key-file problems
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _cache_org(environment_name: str, org_dto: dict[str, Any]) -> tuple[str, str]:
    store.upsert_organization(
        environment_name,
        org_dto["name"],
        org_dto["id"],
        org_dto["displayName"],
        bool(org_dto["active"]),
    )
    return str(org_dto["id"]), str(org_dto["name"])


def _local_sa(environment_name: str, label: str) -> store.Identity:
    identity = store.get_identity(environment_name, label)
    if identity.kind != "SERVICE_ACCOUNT":
        raise ServiceAccountError(
            f"{label!r} is a {identity.kind} identity, not a service account."
        )
    return identity


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


def _sa_row(dto: dict[str, Any], cred: store.ServiceAccountCredential | None) -> dict[str, Any]:
    return {
        "id": dto["id"],
        "display_name": dto.get("displayName", ""),
        "role": dto.get("role", ""),
        "status": dto.get("status", ""),
        "key_version": dto.get("keyVersion", ""),
        "keypair": "provisioned" if dto.get("publicKey") else "none",
        "private_key_path": (cred.private_key_path if cred else None) or "(none)",
    }


@app.command("add")
def add(
    display_name: Annotated[str, typer.Argument(help="Display name.")],
    role: Annotated[
        ServiceAccountRole, typer.Option("--role", help="Service-account role.")
    ],
    org: Annotated[str, typer.Option("--org", help="Organization name.")],
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
    """Create a service account, provision its keypair, store it locally."""
    identity_label = label or _slugify(display_name)
    if not identity_label:
        raise typer.BadParameter(
            "could not derive a label from the display name — pass --label",
            param_hint="--label",
        )
    client_secret = secret or secrets.token_hex(16)

    with _report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        # Fail before any server call if the label is taken (avoids orphaning a
        # fully-provisioned server account with a lost secret).
        _assert_label_free(environment.name, identity_label)

        with api.IdpClient(environment.idp_base_url) as client:
            org_dto = client.get("/v1/organizations", params={"name": org})
        org_id, org_name = _cache_org(environment.name, org_dto)

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
                    "orgId": org_id,
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
        store.set_service_account_credential(
            identity.id,
            environment.name,
            org_name,
            client_secret,
            public_key_path=str(public_path),
            private_key_path=str(private_path),
            key_version=1,
        )

    render(_sa_row(created, None), title="Service account created")
    console.print(f"[bold]Client secret (shown once):[/bold] {client_secret}")


def _assert_label_free(environment_name: str, label: str) -> None:
    try:
        store.get_identity(environment_name, label)
    except store.IdentityNotFoundError:
        return
    raise store.IdentityExistsError(
        f"Identity {label!r} already exists in environment {environment_name!r}. "
        f"Pick another --label."
    )


@app.command("show")
def show(
    label: Annotated[str, typer.Argument(help="Local label of the service account.")],
    reveal_secret: Annotated[
        bool, typer.Option("--reveal-secret", help="Show the stored client secret.")
    ] = False,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Show a service account, merging the server record with local metadata."""
    with _report_errors():
        environment = store.get_environment(env)
        identity = _local_sa(environment.name, label)
        caller = store.resolve_identity(environment.name, as_)
        dto = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.get(f"/v1/service-accounts/{identity.server_id}"),
        )
        cred = store.get_service_account_credential(identity.id)

    if cred is None:
        client_secret = "(not stored)"
    else:
        client_secret = cred.client_secret_plaintext if reveal_secret else "********"

    render(
        {
            "label": identity.label,
            "id": dto["id"],
            "display_name": dto.get("displayName", ""),
            "description": dto.get("description", "") or "",
            "role": dto.get("role", ""),
            "status": dto.get("status", ""),
            "org": cred.org_name if cred else "-",
            "client_secret": client_secret,
            "key_version": dto.get("keyVersion", ""),
            "private_key_path": (cred.private_key_path if cred else None) or "(none)",
            "public_key_path": (cred.public_key_path if cred else None) or "(none)",
        },
        title=f"Service account: {label}",
    )


@app.command("list")
def list_(
    role: Annotated[
        ServiceAccountRole | None, typer.Option("--role", help="Filter by role.")
    ] = None,
    status: Annotated[
        ServiceAccountStatus | None, typer.Option("--status", help="Filter by status.")
    ] = None,
    include_deleted: Annotated[
        bool, typer.Option("--include-deleted", help="Include DELETED accounts.")
    ] = False,
    page: Annotated[int, typer.Option("--page", min=0, help="Page number (0-based).")] = 0,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """List server-side service accounts."""
    with _report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        params: dict[str, Any] = {"page": page}
        if role is not None:
            params["role"] = role.value
        if status is not None:
            params["status"] = status.value
        if include_deleted:
            params["includeDeleted"] = "true"
        body = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.get("/v1/service-accounts", params=params),
        )
        items: list[dict[str, Any]] = body.get("items", [])
        next_cursor = body.get("nextCursor")

    render(
        [
            {
                "id": item["id"],
                "display_name": item.get("displayName", ""),
                "role": item.get("role", ""),
                "status": item.get("status", ""),
                "key_version": item.get("keyVersion", ""),
            }
            for item in items
        ],
        title="Service accounts",
    )
    if next_cursor:
        console.print(f"[dim]more results — rerun with --page {next_cursor}[/dim]")


@app.command("update")
def update(
    label: Annotated[str, typer.Argument(help="Local label of the service account.")],
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
    """Update a service account (partial). Key rotation is `rotate-keys`, not here."""
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

    with _report_errors():
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

    render(_sa_row(dto, store.get_service_account_credential(identity.id)), title="Service account updated")


@app.command("rotate-keys")
def rotate_keys(
    label: Annotated[str, typer.Argument(help="Local label of the service account.")],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Generate a new keypair and hand its public half to the server."""
    with _report_errors():
        environment = store.get_environment(env)
        identity = _local_sa(environment.name, label)
        cred = store.get_service_account_credential(identity.id)
        if cred is None:
            raise ServiceAccountError(f"no local credential recorded for {label!r}.")
        caller = store.resolve_identity(environment.name, as_)

        if cred.key_version is not None:
            current_version = cred.key_version
        else:
            dto = auth.authed_call(
                caller,
                environment.idp_base_url,
                lambda c: c.get(f"/v1/service-accounts/{identity.server_id}"),
            )
            current_version = int(dto["keyVersion"])
        new_version = current_version + 1

        # Stage beside the real keys dir (same filesystem, so the final move is
        # atomic) rather than in the system temp dir.
        keys_root = store.keys_root()
        keys_root.mkdir(parents=True, exist_ok=True)
        key_dir = keys_root / identity.server_id
        staging = Path(
            tempfile.mkdtemp(prefix=f".{identity.server_id}.rotating-", dir=keys_root)
        )
        try:
            new_private, new_public = keys.generate_keypair(staging)
            auth.authed_call(
                caller,
                environment.idp_base_url,
                lambda c: c.patch(
                    f"/v1/service-accounts/{identity.server_id}",
                    json={
                        "publicKey": keys.public_key_b64url(new_public),
                        "keyVersion": new_version,
                    },
                ),
            )
            final_private, final_public = keys.install_keypair(
                new_private, new_public, key_dir
            )
            store.update_service_account_keys(
                identity.id, str(final_public), str(final_private), new_version
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    console.print(
        f"Rotated keys for [bold]{label}[/bold] to key version {new_version}. "
        f"The previous private key is now unusable."
    )


@app.command("delete")
def delete(
    label: Annotated[str, typer.Argument(help="Local label of the service account.")],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Soft-delete server-side, then drop the local identity and key files."""
    with _report_errors():
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
    label: Annotated[str, typer.Argument(help="Local label of the service account.")],
    aud: Annotated[
        str | None,
        typer.Option("--aud", help="Audience URL (default: the env's SA authenticate endpoint)."),
    ] = None,
    env: EnvOption = None,
) -> None:
    """Print a signed short-lived EdDSA JWT assertion for this service account."""
    with _report_errors():
        environment = store.get_environment(env)
        identity = _local_sa(environment.name, label)
        cred = store.get_service_account_credential(identity.id)
        if cred is None or cred.private_key_path is None:
            raise ServiceAccountError(
                f"no private key path recorded for {label!r} — re-import it with "
                f"--private-key-path, or provision a fresh account with "
                f"`vl service-account add`."
            )
        audience = aud or (
            environment.idp_base_url.rstrip("/") + "/auth/service-account/authenticate"
        )
        token = keys.sign_assertion(
            Path(cred.private_key_path),
            subject=identity.server_id,
            audience=audience,
        )
    print(token)
