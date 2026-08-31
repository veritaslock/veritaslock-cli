"""Local SQLite store for `vl`.

Schema grows one phase at a time, each adding a forward migration to ``_MIGRATIONS``
and bumping ``SCHEMA_VERSION`` (§8 of the ``vl env`` spec):

* v1 — ``environment`` table + ``vl env`` (Phase 1).
* v2 — ``organization`` cache table + ``vl org`` (Phase 2).
* v3 — ``identity`` / ``user_credential`` / ``org_membership`` / ``token_cache``
  + ``vl identity`` and ``vl user`` (Phase 3).

On open, every migration between the store's ``PRAGMA user_version`` and
``SCHEMA_VERSION`` is applied in order; a store from a *newer* `vl` is rejected
rather than used against a schema this code doesn't understand.

Everything here is local-only: no network calls, no authentication.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

SCHEMA_VERSION = 3

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
class UserCredential:
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


# --------------------------------------------------------------------------- #
# Connection / schema
# --------------------------------------------------------------------------- #


def _store_path() -> Path:
    override = os.environ.get("VL_STORE_PATH")
    return Path(override).expanduser() if override else DEFAULT_STORE_PATH


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


def set_user_credential(identity_id: int, password_plaintext: str | None) -> None:
    """Set (or clear) the stored password for a USER identity. ``None`` == tier 2."""
    with _store() as conn:
        conn.execute(
            """
            INSERT INTO user_credential (identity_id, password_plaintext)
            VALUES (?, ?)
            ON CONFLICT (identity_id) DO UPDATE SET
                password_plaintext = excluded.password_plaintext
            """,
            (identity_id, password_plaintext),
        )
        conn.commit()


def get_user_credential(identity_id: int) -> UserCredential | None:
    with _store() as conn:
        row = conn.execute(
            "SELECT * FROM user_credential WHERE identity_id = ?", (identity_id,)
        ).fetchone()
        if row is None:
            return None
        return UserCredential(row["identity_id"], row["password_plaintext"])


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
