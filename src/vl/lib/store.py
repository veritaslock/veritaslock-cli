"""Local SQLite store for `vl`.

Phase 1 covers only the ``environment`` table and its helpers. Later phases add
``identity``, ``token_cache``, etc. to the same ``store.db``; each bumps
``SCHEMA_VERSION`` (§8 of the ``vl env`` spec) so a code/schema mismatch is caught
explicitly at store-open time rather than failing confusingly later.

Everything here is local-only: no network calls, no authentication.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

DEFAULT_STORE_PATH = Path.home() / ".config" / "vl" / "store.db"

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

    if version == 0:
        conn.executescript(
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
            """
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        version = SCHEMA_VERSION

    if version != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"store.db is at schema version {version}, but this `vl` supports "
            f"version {SCHEMA_VERSION}. Upgrade `vl`, or point VL_STORE_PATH at a "
            f"different store."
        )

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
