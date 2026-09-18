"""Tests for the `organization` cache + schema migration (vl-org-spec.md §3)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from vl.lib import store


def _open_raw(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _tables(db_path: Path) -> set[str]:
    with _open_raw(db_path) as conn:
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


# --------------------------------------------------------------------------- #
# Schema migration
# --------------------------------------------------------------------------- #


def test_fresh_store_is_at_latest_version_with_both_tables(
    isolated_store: Path,
) -> None:
    store.ensure_local_environment_seeded()

    assert {"environment", "organization"} <= _tables(isolated_store)
    with _open_raw(isolated_store) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION


def test_v1_store_is_migrated_in_place_preserving_data(
    isolated_store: Path,
) -> None:
    # Build a v1-shaped store by hand: environment table only, user_version = 1.
    isolated_store.parent.mkdir(parents=True, exist_ok=True)
    with _open_raw(isolated_store) as conn:
        conn.executescript(
            """
            CREATE TABLE environment (
                name TEXT PRIMARY KEY,
                idp_base_url TEXT NOT NULL,
                cp_base_url TEXT NOT NULL,
                di_base_url TEXT NOT NULL,
                kafka_bootstrap TEXT,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            INSERT INTO environment VALUES
                ('local', 'http://x', 'http://y', 'http://z', NULL, 1, '2026-01-01');
            PRAGMA user_version = 1;
            """
        )
        conn.commit()

    envs = store.list_environments()

    assert [e.name for e in envs] == ["local"]  # existing data intact
    assert envs[0].token_url == ""  # backfilled by the v9 -> v10 migration
    assert envs[0].schema_reg_url == ""
    assert {"organization", "identity"} <= _tables(isolated_store)
    with _open_raw(isolated_store) as conn:
        assert (
            conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
        )


def test_store_from_a_newer_vl_is_rejected(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    with _open_raw(isolated_store) as conn:
        conn.execute("PRAGMA user_version = 999")
        conn.commit()

    with pytest.raises(store.SchemaVersionError):
        store.list_environments()


# --------------------------------------------------------------------------- #
# upsert / get / list
# --------------------------------------------------------------------------- #


def test_upsert_inserts_then_updates_same_row(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()

    first = store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    assert first.server_org_id == "org-1"
    assert first.active is True

    second = store.upsert_organization(
        "local", "globo", "org-1", "Globo Renamed", active=False
    )
    assert second.display_name == "Globo Renamed"
    assert second.active is False
    assert second.synced_at >= first.synced_at

    assert len(store.list_organizations("local")) == 1  # still one row


def test_cache_is_scoped_per_environment(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    store.add_environment("dev", "http://i", "http://c", "http://d", "http://t", "http://s", "k:1")

    store.upsert_organization("local", "globo", "org-local", "Globo", active=True)
    store.upsert_organization("dev", "globo", "org-dev", "Globo", active=True)

    assert store.get_organization("local", "globo").server_org_id == "org-local"
    assert store.get_organization("dev", "globo").server_org_id == "org-dev"


def test_get_unknown_org_raises(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    with pytest.raises(store.OrganizationNotFoundError):
        store.get_organization("local", "nope")


def test_upsert_against_unknown_environment_is_rejected(
    isolated_store: Path,
) -> None:
    store.ensure_local_environment_seeded()
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_organization("ghost-env", "globo", "org-1", "Globo", active=True)
