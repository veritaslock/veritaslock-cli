"""Tests for the `environment` portion of `vl.lib.store` (§9 of the vl env spec)."""

from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

from vl.lib import store


def _open_raw(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------- #
# Auto-seeding
# --------------------------------------------------------------------------- #


def test_auto_seed_creates_single_local_default(isolated_store: Path) -> None:
    envs = store.list_environments()

    assert [e.name for e in envs] == ["local"]
    local = envs[0]
    assert local.is_default is True
    assert local.idp_base_url == "http://localhost:8080"
    assert local.cp_base_url == "http://localhost:8082"
    assert local.di_base_url == "http://localhost:8083"
    assert local.kafka_bootstrap is None
    assert isolated_store.exists()


def test_auto_seed_is_idempotent(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    store.ensure_local_environment_seeded()
    store.list_environments()

    with _open_raw(isolated_store) as conn:
        count = conn.execute("SELECT COUNT(*) FROM environment").fetchone()[0]
    assert count == 1


def test_auto_seed_does_not_recreate_after_user_deletes_local(
    isolated_store: Path,
) -> None:
    # Add a second env, make it default, delete local, then re-open the store.
    store.add_environment("dev", "http://i", "http://c", "http://d")
    store.set_default_environment("dev")
    store.delete_environment("local")

    names = {e.name for e in store.list_environments()}
    assert names == {"dev"}  # not re-seeded just because 'local' is gone


def test_schema_version_is_set(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    with _open_raw(isolated_store) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == store.SCHEMA_VERSION


def test_newer_schema_version_is_rejected(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    with _open_raw(isolated_store) as conn:
        conn.execute("PRAGMA user_version = 999")
        conn.commit()

    with pytest.raises(store.SchemaVersionError):
        store.list_environments()


def test_store_file_is_chmod_600(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    mode = stat.S_IMODE(isolated_store.stat().st_mode)
    assert mode == 0o600


# --------------------------------------------------------------------------- #
# add
# --------------------------------------------------------------------------- #


def test_add_environment(isolated_store: Path) -> None:
    env = store.add_environment(
        "dev",
        "http://idp",
        "http://cp",
        "http://di",
        kafka_bootstrap="localhost:9092",
    )
    assert env.name == "dev"
    assert env.kafka_bootstrap == "localhost:9092"
    assert env.is_default is False  # §5.1 — adding never claims default


def test_add_duplicate_name_is_rejected(isolated_store: Path) -> None:
    store.add_environment("dev", "http://i", "http://c", "http://d")
    with pytest.raises(store.EnvironmentExistsError):
        store.add_environment("dev", "http://x", "http://y", "http://z")


def test_add_duplicate_local_is_rejected(isolated_store: Path) -> None:
    with pytest.raises(store.EnvironmentExistsError):
        store.add_environment("local", "http://i", "http://c", "http://d")


# --------------------------------------------------------------------------- #
# use / default resolution
# --------------------------------------------------------------------------- #


def test_use_moves_the_single_default(isolated_store: Path) -> None:
    store.add_environment("dev", "http://i", "http://c", "http://d")
    store.set_default_environment("dev")

    defaults = [e.name for e in store.list_environments() if e.is_default]
    assert defaults == ["dev"]


def test_use_nonexistent_raises_and_keeps_current_default(
    isolated_store: Path,
) -> None:
    with pytest.raises(store.EnvironmentNotFoundError):
        store.set_default_environment("nope")

    defaults = [e.name for e in store.list_environments() if e.is_default]
    assert defaults == ["local"]  # unchanged


def test_use_is_repeatable_and_always_leaves_exactly_one_default(
    isolated_store: Path,
) -> None:
    store.add_environment("a", "http://i", "http://c", "http://d")
    store.add_environment("b", "http://i", "http://c", "http://d")

    for name in ("a", "b", "a", "local", "b"):
        store.set_default_environment(name)
        defaults = [e.name for e in store.list_environments() if e.is_default]
        assert defaults == [name]


def test_get_environment_by_name(isolated_store: Path) -> None:
    store.add_environment("dev", "http://i", "http://c", "http://d")
    assert store.get_environment("dev").name == "dev"


def test_get_environment_default(isolated_store: Path) -> None:
    assert store.get_environment().name == "local"


def test_get_environment_unknown_name_raises(isolated_store: Path) -> None:
    with pytest.raises(store.EnvironmentNotFoundError):
        store.get_environment("nope")


def test_get_environment_no_default_raises(isolated_store: Path) -> None:
    # Force the degenerate "no default" state directly in the DB.
    store.ensure_local_environment_seeded()
    with _open_raw(isolated_store) as conn:
        conn.execute("UPDATE environment SET is_default = 0")
        conn.commit()

    with pytest.raises(store.NoDefaultEnvironmentError):
        store.get_environment()


# --------------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------------- #


def test_update_changes_only_supplied_fields(isolated_store: Path) -> None:
    store.add_environment(
        "dev", "http://i", "http://c", "http://d", kafka_bootstrap="k:1"
    )
    env = store.update_environment("dev", cp_base_url="http://cp-new")

    assert env.cp_base_url == "http://cp-new"
    assert env.idp_base_url == "http://i"  # untouched
    assert env.di_base_url == "http://d"  # untouched
    assert env.kafka_bootstrap == "k:1"  # untouched


def test_update_nonexistent_raises(isolated_store: Path) -> None:
    with pytest.raises(store.EnvironmentNotFoundError):
        store.update_environment("nope", idp_base_url="http://x")


def test_update_does_not_touch_default_flag(isolated_store: Path) -> None:
    store.add_environment("dev", "http://i", "http://c", "http://d")
    store.update_environment("dev", idp_base_url="http://i2")
    assert store.get_environment().name == "local"


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #


def test_delete_non_default_environment(isolated_store: Path) -> None:
    store.add_environment("dev", "http://i", "http://c", "http://d")
    store.delete_environment("dev")
    assert [e.name for e in store.list_environments()] == ["local"]


def test_delete_default_environment_is_blocked(isolated_store: Path) -> None:
    with pytest.raises(store.EnvironmentInUseError):
        store.delete_environment("local")
    assert [e.name for e in store.list_environments()] == ["local"]


def test_delete_nonexistent_raises(isolated_store: Path) -> None:
    with pytest.raises(store.EnvironmentNotFoundError):
        store.delete_environment("nope")


def test_delete_referenced_environment_raises_typed_error(
    isolated_store: Path,
) -> None:
    """The store's contract: a FK restriction surfaces as EnvironmentInUseError,
    never a raw sqlite3.IntegrityError. Simulated here with a stand-in child
    table until a real referencing table lands in the `vl identity` phase.
    """
    store.add_environment("dev", "http://i", "http://c", "http://d")
    with _open_raw(isolated_store) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            CREATE TABLE _ref (
                id INTEGER PRIMARY KEY,
                environment_name TEXT REFERENCES environment(name)
                    ON UPDATE CASCADE ON DELETE RESTRICT
            )
            """
        )
        conn.execute("INSERT INTO _ref (environment_name) VALUES ('dev')")
        conn.commit()

    with pytest.raises(store.EnvironmentInUseError):
        store.delete_environment("dev")
