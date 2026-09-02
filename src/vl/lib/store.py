"""Local SQLite store for `vl`.

Schema grows one phase at a time, each adding a forward migration to ``_MIGRATIONS``
and bumping ``SCHEMA_VERSION`` (§8 of the ``vl env`` spec):

* v1 — ``environment`` table + ``vl env`` (Phase 1).
* v2 — ``organization`` cache table + ``vl org`` (Phase 2).
* v3 — ``identity`` / ``user_credential`` / ``org_membership`` / ``token_cache``
  + ``vl identity`` and ``vl user`` (Phase 3).
* v4 — ``service_account_credential`` + ``vl service-account`` (Phase 4).
* v5 — rename ``user_credential`` -> ``user_acct``, ``service_account_credential``
  -> ``svc_acct`` (naming-convention cleanup, no schema change).
* v6 — ``team`` cache + ``team_member`` join + ``vl team`` (Phase 6).
* v7 — ``command_history`` ring buffer + ``vl history``.
* v8 — ``svc_acct.role`` column (shown by ``vl svc-acct list`` / ``show``).

On open, every migration between the store's ``PRAGMA user_version`` and
``SCHEMA_VERSION`` is applied in order; a store from a *newer* `vl` is rejected
rather than used against a schema this code doesn't understand.

Everything here is local-only: no network calls, no authentication.
"""

from __future__ import annotations

import os
import shlex
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

SCHEMA_VERSION = 8

# Treat a cached token as expired this many seconds before its real expiry, to
# avoid handing a command a token that dies mid-request.
_TOKEN_SKEW_SECONDS = 30

DEFAULT_STORE_PATH = Path.home() / ".config" / "vl" / "store.db"

# Forward migrations, one per schema version. _MIGRATIONS[i] takes the store from
# version i to version i + 1; a fresh store (user_version 0) runs them all.
_MIGRATIONS: tuple[str, ...] = (
    # 0 -> 1: environment table (Phase 1).
    """
    CREATE TABLE IF NOT EXISTS environment (
        name             TEXT PRIMARY KEY,
        idp_base_url     TEXT NOT NULL,
        cp_base_url      TEXT NOT NULL,
        di_base_url      TEXT NOT NULL,
        kafka_bootstrap  TEXT,
        is_default       INTEGER NOT NULL DEFAULT 0
                         CHECK (is_default IN (0, 1)),
        created_at       TEXT NOT NULL
    );
    """,
    # 1 -> 2: organization cache, scoped per environment (Phase 2).
    """
    CREATE TABLE IF NOT EXISTS organization (
        environment_name  TEXT NOT NULL
                          REFERENCES environment(name)
                          ON UPDATE CASCADE ON DELETE RESTRICT,
        name              TEXT NOT NULL,
        server_org_id     TEXT NOT NULL,
        display_name      TEXT NOT NULL,
        active            INTEGER NOT NULL CHECK (active IN (0, 1)),
        synced_at         TEXT NOT NULL,
        PRIMARY KEY (environment_name, name)
    );
    """,
    # 2 -> 3: identities, their USER credentials, org memberships, token cache (Phase 3).
    """
    CREATE TABLE IF NOT EXISTS identity (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        environment_name  TEXT NOT NULL
                          REFERENCES environment(name)
                          ON UPDATE CASCADE ON DELETE RESTRICT,
        kind              TEXT NOT NULL CHECK (kind IN ('USER', 'SERVICE_ACCOUNT')),
        server_id         TEXT NOT NULL,
        principal_name    TEXT NOT NULL,
        label             TEXT NOT NULL,
        is_default        INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
        created_at        TEXT NOT NULL,
        UNIQUE (environment_name, label)
    );
    CREATE TABLE IF NOT EXISTS user_credential (
        identity_id         INTEGER PRIMARY KEY
                            REFERENCES identity(id) ON DELETE CASCADE,
        password_plaintext  TEXT
    );
    CREATE TABLE IF NOT EXISTS org_membership (
        identity_id       INTEGER NOT NULL
                          REFERENCES identity(id) ON DELETE CASCADE,
        environment_name  TEXT NOT NULL,
        org_name          TEXT NOT NULL,
        role              TEXT NOT NULL,
        synced_at         TEXT NOT NULL,
        PRIMARY KEY (identity_id, environment_name, org_name),
        FOREIGN KEY (environment_name, org_name)
            REFERENCES organization(environment_name, name)
            ON UPDATE CASCADE ON DELETE RESTRICT
    );
    CREATE TABLE IF NOT EXISTS token_cache (
        identity_id  INTEGER PRIMARY KEY
                     REFERENCES identity(id) ON DELETE CASCADE,
        token        TEXT NOT NULL,
        issued_at    TEXT NOT NULL,
        expires_at   TEXT NOT NULL
    );
    """,
    # 3 -> 4: SERVICE_ACCOUNT credential extension (Phase 4). environment_name is
    # denormalised from the parent identity so the composite FK to organization
    # works, same pattern as org_membership.
    """
    CREATE TABLE IF NOT EXISTS service_account_credential (
        identity_id              INTEGER PRIMARY KEY
                                 REFERENCES identity(id) ON DELETE CASCADE,
        environment_name         TEXT NOT NULL,
        org_name                 TEXT NOT NULL,
        client_secret_plaintext  TEXT NOT NULL,
        public_key_path          TEXT,
        private_key_path         TEXT,
        key_version              INTEGER,
        FOREIGN KEY (environment_name, org_name)
            REFERENCES organization(environment_name, name)
            ON UPDATE CASCADE ON DELETE RESTRICT
    );
    """,
    # 4 -> 5: rename the two extension tables to the abbreviated convention.
    # RENAME TO preserves every row in place (passwords, secrets, key paths);
    # a fresh store harmlessly creates-then-renames.
    """
    ALTER TABLE user_credential RENAME TO user_acct;
    ALTER TABLE service_account_credential RENAME TO svc_acct;
    """,
    # 5 -> 6: team cache + team_member join, mirroring organization / org_membership
    # (Phase 6). Additive only.
    """
    CREATE TABLE IF NOT EXISTS team (
        environment_name  TEXT NOT NULL
                          REFERENCES environment(name)
                          ON UPDATE CASCADE ON DELETE RESTRICT,
        org_name          TEXT NOT NULL,
        name              TEXT NOT NULL,
        description       TEXT,
        server_team_id    TEXT NOT NULL,
        created_by        TEXT,
        created_at        TEXT NOT NULL,
        synced_at         TEXT NOT NULL,
        PRIMARY KEY (environment_name, org_name, name),
        FOREIGN KEY (environment_name, org_name)
            REFERENCES organization(environment_name, name)
            ON UPDATE CASCADE ON DELETE RESTRICT
    );
    CREATE TABLE IF NOT EXISTS team_member (
        identity_id       INTEGER NOT NULL
                          REFERENCES identity(id) ON DELETE CASCADE,
        environment_name  TEXT NOT NULL,
        org_name          TEXT NOT NULL,
        team_name         TEXT NOT NULL,
        role              TEXT NOT NULL,
        synced_at         TEXT NOT NULL,
        PRIMARY KEY (identity_id, environment_name, org_name, team_name),
        FOREIGN KEY (environment_name, org_name, team_name)
            REFERENCES team(environment_name, org_name, name)
            ON DELETE CASCADE
    );
    """,
    # 6 -> 7: local command-history ring buffer for `vl history`. Local-only and
    # best-effort — writes here are swallowed on failure and never fail the
    # command being logged. Trimmed to the last _HISTORY_KEEP rows on each write.
    """
    CREATE TABLE IF NOT EXISTS command_history (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        ran_at  TEXT NOT NULL,
        argv    TEXT NOT NULL
    );
    """,
    # 7 -> 8: cache the service account's role locally so `vl svc-acct list` /
    # `show` can display it without a server call. Nullable — rows written by an
    # older `vl` stay NULL until the account is next re-cached or updated.
    """
    ALTER TABLE svc_acct ADD COLUMN role TEXT;
    """,
)

# Auto-seeded on first store-open so a fresh install works with zero setup (§4).
_LOCAL_SEED = {
    "name": "local",
    "idp_base_url": "http://localhost:8080",
    "cp_base_url": "http://localhost:8082",
    "di_base_url": "http://localhost:8083",
    "kafka_bootstrap": None,
}


class StoreError(Exception):
    """Base for store errors that should surface as a clean CLI message."""


class EnvironmentNotFoundError(StoreError):
    """Named environment does not exist in the store."""


class EnvironmentExistsError(StoreError):
    """An environment with that name already exists."""


class EnvironmentInUseError(StoreError):
    """Environment can't be deleted — it's the default or something references it."""


class NoDefaultEnvironmentError(StoreError):
    """No ``--env`` given and no default environment is set."""


class SchemaVersionError(StoreError):
    """store.db was written by a newer (or otherwise incompatible) `vl`."""


class OrganizationNotFoundError(StoreError):
    """Named organization is not in the local cache."""


class IdentityNotFoundError(StoreError):
    """No stored identity with that label in the environment."""


class IdentityExistsError(StoreError):
    """An identity with that label already exists in the environment."""


class NoResolvedIdentityError(StoreError):
    """No ``--as`` / ``VL_IDENTITY`` / default identity to run an authed command as."""


class TeamNotFoundError(StoreError):
    """Named team is not in the local cache."""


@dataclass(frozen=True)
class Environment:
    """A resolved environment definition."""

    name: str
    idp_base_url: str
    cp_base_url: str
    di_base_url: str
    kafka_bootstrap: str | None
    is_default: bool
    created_at: str


@dataclass(frozen=True)
class Organization:
    """A cached organization row (local mirror of the server's, not a source of truth)."""

    environment_name: str
    name: str
    server_org_id: str
    display_name: str
    active: bool
    synced_at: str


@dataclass(frozen=True)
class Identity:
    """A stored identity `vl` can act as."""

    id: int
    environment_name: str
    kind: str
    server_id: str
    principal_name: str
    label: str
    is_default: bool
    created_at: str


@dataclass(frozen=True)
class UserAcct:
    """USER-kind extension of an identity. ``password_plaintext`` None == tier 2 (§7)."""

    identity_id: int
    password_plaintext: str | None


@dataclass(frozen=True)
class OrgMembership:
    """A cached org membership for a stored identity."""

    identity_id: int
    environment_name: str
    org_name: str
    role: str
    synced_at: str


@dataclass(frozen=True)
class Token:
    """A cached, still-valid JWT for a stored identity."""

    identity_id: int
    token: str
    issued_at: str
    expires_at: str


@dataclass(frozen=True)
class SvcAcct:
    """SERVICE_ACCOUNT extension of an identity. ``client_secret_plaintext`` is always set."""

    identity_id: int
    environment_name: str
    org_name: str
    client_secret_plaintext: str
    public_key_path: str | None
    private_key_path: str | None
    key_version: int | None
    role: str | None = None


@dataclass(frozen=True)
class Team:
    """A cached team row (local mirror of the server's, not a source of truth)."""

    environment_name: str
    org_name: str
    name: str
    description: str | None
    server_team_id: str
    created_by: str | None
    created_at: str
    synced_at: str


@dataclass(frozen=True)
class TeamMember:
    """A cached team membership for a stored identity."""

    identity_id: int
    environment_name: str
    org_name: str
    team_name: str
    role: str
    synced_at: str


# --------------------------------------------------------------------------- #
# Connection / schema
# --------------------------------------------------------------------------- #


def _store_path() -> Path:
    override = os.environ.get("VL_STORE_PATH")
    return Path(override).expanduser() if override else DEFAULT_STORE_PATH


def keys_root() -> Path:
    """Directory holding per-service-account key material, beside ``store.db``."""
    return _store_path().parent / "keys"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _store() -> Iterator[sqlite3.Connection]:
    """Open the store, applying schema + seed, and always close the handle."""
    path = _store_path()

    if not path.parent.exists():
        path.parent.mkdir(parents=True)
        os.chmod(path.parent, 0o700)

    newly_created = not path.exists()
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if newly_created:
            os.chmod(path, 0o600)
        _init_schema(conn)
        yield conn
    finally:
        conn.close()


def _init_schema(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])

    if version > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"store.db is at schema version {version}, but this `vl` only "
            f"supports up to version {SCHEMA_VERSION}. Upgrade `vl`, or point "
            f"VL_STORE_PATH at a different store."
        )

    for current in range(version, SCHEMA_VERSION):
        conn.executescript(_MIGRATIONS[current])
        conn.execute(f"PRAGMA user_version = {current + 1}")
        conn.commit()

    _seed_local(conn)


def _seed_local(conn: sqlite3.Connection) -> None:
    """Create the ``local`` row if the table is empty (idempotent)."""
    if conn.execute("SELECT COUNT(*) FROM environment").fetchone()[0]:
        return
    conn.execute(
        """
        INSERT INTO environment (name, idp_base_url, cp_base_url, di_base_url,
                                 kafka_bootstrap, is_default, created_at)
        VALUES (:name, :idp_base_url, :cp_base_url, :di_base_url,
                :kafka_bootstrap, 1, :created_at)
        """,
        {**_LOCAL_SEED, "created_at": _now()},
    )
    conn.commit()


def _row_to_env(row: sqlite3.Row) -> Environment:
    return Environment(
        name=row["name"],
        idp_base_url=row["idp_base_url"],
        cp_base_url=row["cp_base_url"],
        di_base_url=row["di_base_url"],
        kafka_bootstrap=row["kafka_bootstrap"],
        is_default=bool(row["is_default"]),
        created_at=row["created_at"],
    )


def _find(conn: sqlite3.Connection, name: str) -> Environment | None:
    row = conn.execute(
        "SELECT * FROM environment WHERE name = ?", (name,)
    ).fetchone()
    return _row_to_env(row) if row is not None else None


# --------------------------------------------------------------------------- #
# Public API — environment portion
# --------------------------------------------------------------------------- #


def ensure_local_environment_seeded() -> None:
    """Open the store once so the schema exists and ``local`` is seeded."""
    with _store():
        pass


def add_environment(
    name: str,
    idp_base_url: str,
    cp_base_url: str,
    di_base_url: str,
    kafka_bootstrap: str | None = None,
) -> Environment:
    """Insert a new environment. Does not make it the default."""
    with _store() as conn:
        if _find(conn, name) is not None:
            raise EnvironmentExistsError(
                f"Environment {name!r} already exists. Use "
                f"`vl env update {name} ...` to change it."
            )
        conn.execute(
            """
            INSERT INTO environment (name, idp_base_url, cp_base_url, di_base_url,
                                     kafka_bootstrap, is_default, created_at)
            VALUES (:name, :idp_base_url, :cp_base_url, :di_base_url,
                    :kafka_bootstrap, 0, :created_at)
            """,
            {
                "name": name,
                "idp_base_url": idp_base_url,
                "cp_base_url": cp_base_url,
                "di_base_url": di_base_url,
                "kafka_bootstrap": kafka_bootstrap,
                "created_at": _now(),
            },
        )
        conn.commit()
        env = _find(conn, name)
        assert env is not None  # just inserted
        return env


def list_environments() -> list[Environment]:
    """All environments, ordered by name."""
    with _store() as conn:
        rows = conn.execute(
            "SELECT * FROM environment ORDER BY name"
        ).fetchall()
        return [_row_to_env(row) for row in rows]


def get_environment(name: str | None = None) -> Environment:
    """Resolve an environment.

    ``name`` given  -> that environment, or ``EnvironmentNotFoundError``.
    ``name`` is None -> the default environment, or ``NoDefaultEnvironmentError``.
    """
    with _store() as conn:
        if name is not None:
            env = _find(conn, name)
            if env is None:
                raise EnvironmentNotFoundError(
                    f"No environment named {name!r}. Run `vl env list` to see what "
                    f"exists, or `vl env add {name} ...` to create it."
                )
            return env

        row = conn.execute(
            "SELECT * FROM environment WHERE is_default = 1"
        ).fetchone()
        if row is None:
            raise NoDefaultEnvironmentError(
                "No default environment is set. Run `vl env use <name>` to pick "
                "one (`vl env list` shows what's available)."
            )
        return _row_to_env(row)


def set_default_environment(name: str) -> None:
    """Make ``name`` the sole default environment."""
    with _store() as conn:
        if _find(conn, name) is None:
            raise EnvironmentNotFoundError(
                f"No environment named {name!r}. Run `vl env list` to see what "
                f"exists."
            )
        # One statement: sets 1 on the match and 0 on every other row, so there is
        # never a window with zero or two defaults.
        conn.execute(
            "UPDATE environment SET is_default = CASE WHEN name = ? THEN 1 ELSE 0 END",
            (name,),
        )
        conn.commit()


def update_environment(
    name: str,
    idp_base_url: str | None = None,
    cp_base_url: str | None = None,
    di_base_url: str | None = None,
    kafka_bootstrap: str | None = None,
) -> Environment:
    """Partial update — only the non-``None`` fields are changed."""
    fields = {
        key: value
        for key, value in {
            "idp_base_url": idp_base_url,
            "cp_base_url": cp_base_url,
            "di_base_url": di_base_url,
            "kafka_bootstrap": kafka_bootstrap,
        }.items()
        if value is not None
    }

    with _store() as conn:
        if _find(conn, name) is None:
            raise EnvironmentNotFoundError(
                f"No environment named {name!r}. Run `vl env list` to see what "
                f"exists."
            )
        if fields:
            assignments = ", ".join(f"{key} = :{key}" for key in fields)
            conn.execute(
                f"UPDATE environment SET {assignments} WHERE name = :name",
                {**fields, "name": name},
            )
            conn.commit()
        env = _find(conn, name)
        assert env is not None
        return env


def delete_environment(name: str) -> None:
    """Delete an environment. Refuses to delete the default or a referenced one."""
    with _store() as conn:
        env = _find(conn, name)
        if env is None:
            raise EnvironmentNotFoundError(
                f"No environment named {name!r}. Run `vl env list` to see what "
                f"exists."
            )
        if env.is_default:
            raise EnvironmentInUseError(
                f"{name!r} is the default environment. Run `vl env use <other>` "
                f"first, then delete it."
            )
        try:
            conn.execute("DELETE FROM environment WHERE name = ?", (name,))
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise EnvironmentInUseError(
                f"Environment {name!r} still has resources referencing it. Remove "
                f"those first, then delete the environment."
            ) from exc


# --------------------------------------------------------------------------- #
# Public API — organization cache (Phase 2)
# --------------------------------------------------------------------------- #


def _row_to_org(row: sqlite3.Row) -> Organization:
    return Organization(
        environment_name=row["environment_name"],
        name=row["name"],
        server_org_id=row["server_org_id"],
        display_name=row["display_name"],
        active=bool(row["active"]),
        synced_at=row["synced_at"],
    )


def upsert_organization(
    environment_name: str,
    name: str,
    server_org_id: str,
    display_name: str,
    active: bool,
) -> Organization:
    """Insert or refresh a cached organization row, stamping ``synced_at``.

    The cache is a local mirror, never a source of truth — callers upsert it as a
    side effect of a successful `vl org` call against the server.
    """
    with _store() as conn:
        conn.execute(
            """
            INSERT INTO organization (environment_name, name, server_org_id,
                                      display_name, active, synced_at)
            VALUES (:environment_name, :name, :server_org_id, :display_name,
                    :active, :synced_at)
            ON CONFLICT (environment_name, name) DO UPDATE SET
                server_org_id = excluded.server_org_id,
                display_name  = excluded.display_name,
                active        = excluded.active,
                synced_at     = excluded.synced_at
            """,
            {
                "environment_name": environment_name,
                "name": name,
                "server_org_id": server_org_id,
                "display_name": display_name,
                "active": 1 if active else 0,
                "synced_at": _now(),
            },
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM organization WHERE environment_name = ? AND name = ?",
            (environment_name, name),
        ).fetchone()
        assert row is not None  # just upserted
        return _row_to_org(row)


def get_organization(environment_name: str, name: str) -> Organization:
    """A cached organization, or ``OrganizationNotFoundError``."""
    with _store() as conn:
        row = conn.execute(
            "SELECT * FROM organization WHERE environment_name = ? AND name = ?",
            (environment_name, name),
        ).fetchone()
        if row is None:
            raise OrganizationNotFoundError(
                f"No cached organization {name!r} in environment "
                f"{environment_name!r}. Run `vl org show {name}` first."
            )
        return _row_to_org(row)


def list_organizations(environment_name: str) -> list[Organization]:
    """All cached organizations for an environment, ordered by name."""
    with _store() as conn:
        rows = conn.execute(
            "SELECT * FROM organization WHERE environment_name = ? ORDER BY name",
            (environment_name,),
        ).fetchall()
        return [_row_to_org(row) for row in rows]


# --------------------------------------------------------------------------- #
# Public API — identities, credentials, memberships, token cache (Phase 3)
# --------------------------------------------------------------------------- #

IdentityKind = Literal["USER", "SERVICE_ACCOUNT"]


def _row_to_identity(row: sqlite3.Row) -> Identity:
    return Identity(
        id=row["id"],
        environment_name=row["environment_name"],
        kind=row["kind"],
        server_id=row["server_id"],
        principal_name=row["principal_name"],
        label=row["label"],
        is_default=bool(row["is_default"]),
        created_at=row["created_at"],
    )


def _identity_row(
    conn: sqlite3.Connection, environment: str, label: str
) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM identity WHERE environment_name = ? AND label = ?",
        (environment, label),
    ).fetchone()
    return row


def add_identity(
    environment: str,
    kind: IdentityKind,
    server_id: str,
    principal_name: str,
    label: str,
) -> Identity:
    """Create an identity row. Does not make it the environment's default."""
    with _store() as conn:
        if _identity_row(conn, environment, label) is not None:
            raise IdentityExistsError(
                f"Identity {label!r} already exists in environment "
                f"{environment!r}. Pick another --label."
            )
        cursor = conn.execute(
            """
            INSERT INTO identity (environment_name, kind, server_id,
                                  principal_name, label, is_default, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (environment, kind, server_id, principal_name, label, _now()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM identity WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        assert row is not None  # just inserted
        return _row_to_identity(row)


def get_identity(environment: str, label: str) -> Identity:
    """A stored identity by label, or ``IdentityNotFoundError``."""
    with _store() as conn:
        row = _identity_row(conn, environment, label)
        if row is None:
            raise IdentityNotFoundError(
                f"No identity {label!r} in environment {environment!r}. Run "
                f"`vl identity list` to see what's stored."
            )
        return _row_to_identity(row)


def get_identity_by_server_id(
    environment: str, server_id: str, kind: IdentityKind
) -> Identity | None:
    """A stored identity by its server-assigned id, or ``None``."""
    with _store() as conn:
        row = conn.execute(
            "SELECT * FROM identity "
            "WHERE environment_name = ? AND server_id = ? AND kind = ?",
            (environment, server_id, kind),
        ).fetchone()
        return _row_to_identity(row) if row is not None else None


def get_identity_by_principal(
    environment: str, principal_name: str, kind: IdentityKind
) -> Identity | None:
    """A stored identity by principal (username / client id), or ``None``.

    Used by ``vl identity login`` to reuse an existing row regardless of label.
    """
    with _store() as conn:
        row = conn.execute(
            "SELECT * FROM identity "
            "WHERE environment_name = ? AND principal_name = ? AND kind = ?",
            (environment, principal_name, kind),
        ).fetchone()
        return _row_to_identity(row) if row is not None else None


def list_identities(
    environment: str, kind: IdentityKind | None = None
) -> list[Identity]:
    """Stored identities for an environment, ordered by label."""
    with _store() as conn:
        if kind is None:
            rows = conn.execute(
                "SELECT * FROM identity WHERE environment_name = ? ORDER BY label",
                (environment,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM identity "
                "WHERE environment_name = ? AND kind = ? ORDER BY label",
                (environment, kind),
            ).fetchall()
        return [_row_to_identity(row) for row in rows]


def delete_identity(environment: str, label: str) -> None:
    """Delete an identity (cascades to credential, memberships, cached token)."""
    with _store() as conn:
        row = _identity_row(conn, environment, label)
        if row is None:
            raise IdentityNotFoundError(
                f"No identity {label!r} in environment {environment!r}."
            )
        conn.execute("DELETE FROM identity WHERE id = ?", (row["id"],))
        conn.commit()


def set_default_identity(environment: str, label: str) -> None:
    """Make ``label`` the sole default identity for its environment."""
    with _store() as conn:
        if _identity_row(conn, environment, label) is None:
            raise IdentityNotFoundError(
                f"No identity {label!r} in environment {environment!r}."
            )
        conn.execute(
            "UPDATE identity SET is_default = CASE WHEN label = :label THEN 1 ELSE 0 END "
            "WHERE environment_name = :env",
            {"label": label, "env": environment},
        )
        conn.commit()


def resolve_identity(environment: str, explicit_label: str | None) -> Identity:
    """Resolve the identity a command runs as (§8.3): ``--as`` > ``VL_IDENTITY`` > default."""
    label = explicit_label or os.environ.get("VL_IDENTITY") or None
    with _store() as conn:
        if label is not None:
            row = _identity_row(conn, environment, label)
            if row is None:
                source = "--as" if explicit_label else "VL_IDENTITY"
                raise IdentityNotFoundError(
                    f"No identity {label!r} in environment {environment!r} "
                    f"(from {source}). Run `vl identity list`."
                )
            return _row_to_identity(row)

        row = conn.execute(
            "SELECT * FROM identity WHERE environment_name = ? AND is_default = 1",
            (environment,),
        ).fetchone()
        if row is None:
            raise NoResolvedIdentityError(
                f"No identity selected for environment {environment!r}. Pass "
                f"`--as <label>`, set VL_IDENTITY, or run `vl identity use <label>`."
            )
        return _row_to_identity(row)


def set_user_acct(identity_id: int, password_plaintext: str | None) -> None:
    """Set (or clear) the stored password for a USER identity. ``None`` == tier 2."""
    with _store() as conn:
        conn.execute(
            """
            INSERT INTO user_acct (identity_id, password_plaintext)
            VALUES (?, ?)
            ON CONFLICT (identity_id) DO UPDATE SET
                password_plaintext = excluded.password_plaintext
            """,
            (identity_id, password_plaintext),
        )
        conn.commit()


def get_user_acct(identity_id: int) -> UserAcct | None:
    with _store() as conn:
        row = conn.execute(
            "SELECT * FROM user_acct WHERE identity_id = ?", (identity_id,)
        ).fetchone()
        if row is None:
            return None
        return UserAcct(row["identity_id"], row["password_plaintext"])


def upsert_org_membership(
    identity_id: int, environment: str, org_name: str, role: str
) -> None:
    """Insert or refresh a cached org membership, stamping ``synced_at``."""
    with _store() as conn:
        conn.execute(
            """
            INSERT INTO org_membership (identity_id, environment_name, org_name,
                                        role, synced_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (identity_id, environment_name, org_name) DO UPDATE SET
                role = excluded.role,
                synced_at = excluded.synced_at
            """,
            (identity_id, environment, org_name, role, _now()),
        )
        conn.commit()


def list_org_memberships(identity_id: int) -> list[OrgMembership]:
    with _store() as conn:
        rows = conn.execute(
            "SELECT * FROM org_membership WHERE identity_id = ? ORDER BY org_name",
            (identity_id,),
        ).fetchall()
        return [
            OrgMembership(
                identity_id=row["identity_id"],
                environment_name=row["environment_name"],
                org_name=row["org_name"],
                role=row["role"],
                synced_at=row["synced_at"],
            )
            for row in rows
        ]


def default_org_for_identity(identity: Identity) -> str | None:
    """The org an identity implicitly acts in, or ``None`` if it's ambiguous.

    SERVICE_ACCOUNT: its single org. USER: its sole cached org membership, or
    ``None`` when it has zero or more than one.
    """
    if identity.kind == "SERVICE_ACCOUNT":
        cred = get_svc_acct(identity.id)
        return cred.org_name if cred is not None else None
    names = {m.org_name for m in list_org_memberships(identity.id)}
    return next(iter(names)) if len(names) == 1 else None


def get_cached_token(identity_id: int) -> Token | None:
    """A cached token for the identity — ``None`` if absent or (near) expired."""
    with _store() as conn:
        row = conn.execute(
            "SELECT * FROM token_cache WHERE identity_id = ?", (identity_id,)
        ).fetchone()
    if row is None:
        return None
    expires_at = datetime.fromisoformat(row["expires_at"])
    if expires_at <= datetime.now(timezone.utc) + timedelta(
        seconds=_TOKEN_SKEW_SECONDS
    ):
        return None
    return Token(row["identity_id"], row["token"], row["issued_at"], row["expires_at"])


def set_cached_token(
    identity_id: int, token: str, issued_at: datetime, expires_at: datetime
) -> None:
    with _store() as conn:
        conn.execute(
            """
            INSERT INTO token_cache (identity_id, token, issued_at, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (identity_id) DO UPDATE SET
                token = excluded.token,
                issued_at = excluded.issued_at,
                expires_at = excluded.expires_at
            """,
            (identity_id, token, issued_at.isoformat(), expires_at.isoformat()),
        )
        conn.commit()


def clear_cached_token(identity_id: int) -> None:
    """Drop any cached token — used after a mid-command ``401`` (§7 step 4)."""
    with _store() as conn:
        conn.execute(
            "DELETE FROM token_cache WHERE identity_id = ?", (identity_id,)
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# Public API — service-account credentials (Phase 4)
# --------------------------------------------------------------------------- #


def _row_to_svc_acct(row: sqlite3.Row) -> SvcAcct:
    return SvcAcct(
        identity_id=row["identity_id"],
        environment_name=row["environment_name"],
        org_name=row["org_name"],
        client_secret_plaintext=row["client_secret_plaintext"],
        public_key_path=row["public_key_path"],
        private_key_path=row["private_key_path"],
        key_version=row["key_version"],
        # `role` arrived in v8 — tolerate its absence on a store pinned older.
        role=row["role"] if "role" in row.keys() else None,
    )


def set_svc_acct(
    identity_id: int,
    environment: str,
    org_name: str,
    client_secret_plaintext: str,
    *,
    public_key_path: str | None = None,
    private_key_path: str | None = None,
    key_version: int | None = None,
    role: str | None = None,
) -> None:
    """Insert or replace a SERVICE_ACCOUNT identity's credential row."""
    with _store() as conn:
        conn.execute(
            """
            INSERT INTO svc_acct
                (identity_id, environment_name, org_name, client_secret_plaintext,
                 public_key_path, private_key_path, key_version, role)
            VALUES (:identity_id, :environment_name, :org_name, :secret,
                    :public_key_path, :private_key_path, :key_version, :role)
            ON CONFLICT (identity_id) DO UPDATE SET
                environment_name        = excluded.environment_name,
                org_name                = excluded.org_name,
                client_secret_plaintext = excluded.client_secret_plaintext,
                public_key_path         = excluded.public_key_path,
                private_key_path        = excluded.private_key_path,
                key_version             = excluded.key_version,
                role                    = excluded.role
            """,
            {
                "identity_id": identity_id,
                "environment_name": environment,
                "org_name": org_name,
                "secret": client_secret_plaintext,
                "public_key_path": public_key_path,
                "private_key_path": private_key_path,
                "key_version": key_version,
                "role": role,
            },
        )
        conn.commit()


def update_svc_acct_role(identity_id: int, role: str) -> None:
    """Refresh a SERVICE_ACCOUNT credential's cached role (used after `update --role`)."""
    with _store() as conn:
        conn.execute(
            "UPDATE svc_acct SET role = ? WHERE identity_id = ?", (role, identity_id)
        )
        conn.commit()


def refresh_svc_acct_server_fields(
    identity_id: int, *, role: str | None = None, key_version: int | None = None
) -> None:
    """Sync the server-owned columns (``role``, ``key_version``) of a cached
    credential from a fresh ``--remote`` / ``--all`` read. A ``None`` argument
    leaves that column untouched; a missing credential row is a no-op.
    """
    with _store() as conn:
        conn.execute(
            """
            UPDATE svc_acct
               SET role        = COALESCE(:role, role),
                   key_version = COALESCE(:key_version, key_version)
             WHERE identity_id = :identity_id
            """,
            {
                "role": role,
                "key_version": key_version,
                "identity_id": identity_id,
            },
        )
        conn.commit()


def get_svc_acct(
    identity_id: int,
) -> SvcAcct | None:
    with _store() as conn:
        row = conn.execute(
            "SELECT * FROM svc_acct WHERE identity_id = ?",
            (identity_id,),
        ).fetchone()
        return _row_to_svc_acct(row) if row is not None else None


def update_svc_acct_keys(
    identity_id: int,
    public_key_path: str,
    private_key_path: str,
    key_version: int,
) -> None:
    """Point a SERVICE_ACCOUNT credential at freshly rotated key material."""
    with _store() as conn:
        conn.execute(
            """
            UPDATE svc_acct
               SET public_key_path = ?, private_key_path = ?, key_version = ?
             WHERE identity_id = ?
            """,
            (public_key_path, private_key_path, key_version, identity_id),
        )
        conn.commit()


def update_svc_acct_secret(
    identity_id: int, client_secret_plaintext: str, key_version: int | None
) -> None:
    """Refresh a SERVICE_ACCOUNT credential's symmetric secret (used after rotate)."""
    with _store() as conn:
        conn.execute(
            """
            UPDATE svc_acct
               SET client_secret_plaintext = ?, key_version = ?
             WHERE identity_id = ?
            """,
            (client_secret_plaintext, key_version, identity_id),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# Public API — team cache + team memberships (Phase 6)
# --------------------------------------------------------------------------- #


def _row_to_team(row: sqlite3.Row) -> Team:
    return Team(
        environment_name=row["environment_name"],
        org_name=row["org_name"],
        name=row["name"],
        description=row["description"],
        server_team_id=row["server_team_id"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        synced_at=row["synced_at"],
    )


def upsert_team(
    environment_name: str,
    org_name: str,
    name: str,
    server_team_id: str,
    *,
    description: str | None = None,
    created_by: str | None = None,
    created_at: str | None = None,
) -> Team:
    """Insert or refresh a cached team row, stamping ``synced_at``."""
    with _store() as conn:
        conn.execute(
            """
            INSERT INTO team (environment_name, org_name, name, description,
                              server_team_id, created_by, created_at, synced_at)
            VALUES (:environment_name, :org_name, :name, :description,
                    :server_team_id, :created_by,
                    COALESCE(:created_at, :now), :now)
            ON CONFLICT (environment_name, org_name, name) DO UPDATE SET
                description    = excluded.description,
                server_team_id = excluded.server_team_id,
                created_by     = COALESCE(excluded.created_by, team.created_by),
                created_at     = COALESCE(excluded.created_at, team.created_at),
                synced_at      = excluded.synced_at
            """,
            {
                "environment_name": environment_name,
                "org_name": org_name,
                "name": name,
                "description": description,
                "server_team_id": server_team_id,
                "created_by": created_by,
                "created_at": created_at,
                "now": _now(),
            },
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM team WHERE environment_name = ? AND org_name = ? AND name = ?",
            (environment_name, org_name, name),
        ).fetchone()
        assert row is not None  # just upserted
        return _row_to_team(row)


def get_team(environment_name: str, org_name: str, name: str) -> Team:
    """A cached team, or ``TeamNotFoundError``."""
    team = get_team_or_none(environment_name, org_name, name)
    if team is None:
        raise TeamNotFoundError(
            f"No cached team {name!r} in {org_name!r} (environment "
            f"{environment_name!r}). Run `vl team show {org_name} {name}` first."
        )
    return team


def get_team_or_none(
    environment_name: str, org_name: str, name: str
) -> Team | None:
    with _store() as conn:
        row = conn.execute(
            "SELECT * FROM team WHERE environment_name = ? AND org_name = ? AND name = ?",
            (environment_name, org_name, name),
        ).fetchone()
        return _row_to_team(row) if row is not None else None


def list_teams(
    environment_name: str, org_name: str | None = None
) -> list[Team]:
    """Cached teams for an environment, optionally scoped to one org."""
    with _store() as conn:
        if org_name is None:
            rows = conn.execute(
                "SELECT * FROM team WHERE environment_name = ? "
                "ORDER BY org_name, name",
                (environment_name,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM team WHERE environment_name = ? AND org_name = ? "
                "ORDER BY name",
                (environment_name, org_name),
            ).fetchall()
        return [_row_to_team(row) for row in rows]


def delete_team(environment_name: str, org_name: str, name: str) -> None:
    """Drop a cached team (cascades to its ``team_member`` rows)."""
    with _store() as conn:
        cursor = conn.execute(
            "DELETE FROM team WHERE environment_name = ? AND org_name = ? AND name = ?",
            (environment_name, org_name, name),
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise TeamNotFoundError(
                f"No cached team {name!r} in {org_name!r} (environment "
                f"{environment_name!r})."
            )


def upsert_team_member(
    identity_id: int,
    environment_name: str,
    org_name: str,
    team_name: str,
    role: str,
) -> None:
    """Insert or refresh a cached team membership, stamping ``synced_at``."""
    with _store() as conn:
        conn.execute(
            """
            INSERT INTO team_member (identity_id, environment_name, org_name,
                                     team_name, role, synced_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (identity_id, environment_name, org_name, team_name)
            DO UPDATE SET role = excluded.role, synced_at = excluded.synced_at
            """,
            (identity_id, environment_name, org_name, team_name, role, _now()),
        )
        conn.commit()


def list_team_memberships(identity_id: int) -> list[TeamMember]:
    with _store() as conn:
        rows = conn.execute(
            "SELECT * FROM team_member WHERE identity_id = ? ORDER BY org_name, team_name",
            (identity_id,),
        ).fetchall()
        return [
            TeamMember(
                identity_id=row["identity_id"],
                environment_name=row["environment_name"],
                org_name=row["org_name"],
                team_name=row["team_name"],
                role=row["role"],
                synced_at=row["synced_at"],
            )
            for row in rows
        ]


def delete_team_member(
    identity_id: int, environment_name: str, org_name: str, team_name: str
) -> None:
    with _store() as conn:
        conn.execute(
            "DELETE FROM team_member WHERE identity_id = ? AND environment_name = ? "
            "AND org_name = ? AND team_name = ?",
            (identity_id, environment_name, org_name, team_name),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# Command history (`vl history`)
# --------------------------------------------------------------------------- #

# How many recent invocations the ring buffer retains.
_HISTORY_KEEP = 200

# Options whose following value (or `=value`) is a credential and must never be
# written to the history table.
_HISTORY_SECRET_OPTS = frozenset(
    {"--password", "--secret", "--client-id", "--client-secret", "--current-password"}
)


@dataclass(frozen=True)
class HistoryEntry:
    """One recorded `vl` invocation."""

    ran_at: str
    command: str


def _redact_args(args: Sequence[str]) -> list[str]:
    """Shell-quote args, replacing credential values with ``***``."""
    out: list[str] = []
    redact_next = False
    for arg in args:
        if redact_next:
            out.append("***")
            redact_next = False
            continue
        name = arg.split("=", 1)[0]
        if name in _HISTORY_SECRET_OPTS:
            if "=" in arg:
                out.append(f"{name}=***")
            else:
                out.append(name)
                redact_next = True
            continue
        out.append(shlex.quote(arg))
    return out


def record_command(args: Sequence[str]) -> None:
    """Append one invocation to the local history ring buffer (best-effort).

    Credential-bearing option values are redacted first. Any failure — read-only
    store, locked db, schema mismatch — is swallowed: history is a convenience and
    must never fail the command it is logging.
    """
    try:
        rendered = "vl " + " ".join(_redact_args(args)) if args else "vl"
        with _store() as conn:
            conn.execute(
                "INSERT INTO command_history (ran_at, argv) VALUES (?, ?)",
                (_now(), rendered),
            )
            conn.execute(
                "DELETE FROM command_history WHERE id <= "
                "(SELECT MAX(id) FROM command_history) - ?",
                (_HISTORY_KEEP,),
            )
            conn.commit()
    except (sqlite3.Error, OSError, StoreError):
        pass


def list_command_history(limit: int) -> list[HistoryEntry]:
    """The ``limit`` most recent invocations, oldest first.

    ``vl history`` calls are filtered out here as well as at record time, so a
    store written by an older `vl` (which did log them) still reads clean.
    """
    with _store() as conn:
        rows = conn.execute(
            "SELECT ran_at, argv FROM command_history "
            "WHERE argv <> 'vl history' AND argv NOT LIKE 'vl history %' "
            "ORDER BY id DESC LIMIT ?",
            (max(limit, 0),),
        ).fetchall()
    return [
        HistoryEntry(ran_at=row["ran_at"], command=row["argv"])
        for row in reversed(rows)
    ]
