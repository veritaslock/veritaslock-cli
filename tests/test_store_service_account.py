"""Tests for service_account_credential + the v3 -> v4 migration (Phase 4)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from vl.lib import store


def _seed() -> store.Identity:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    return store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "sys")


def test_v4_table_exists_and_version(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    with sqlite3.connect(isolated_store) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert "service_account_credential" in tables
    assert version == store.SCHEMA_VERSION == 4


def test_v3_store_migrates_to_v4(isolated_store: Path) -> None:
    isolated_store.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(isolated_store) as conn:
        conn.executescript(
            """
            CREATE TABLE environment (
                name TEXT PRIMARY KEY, idp_base_url TEXT NOT NULL, cp_base_url TEXT NOT NULL,
                di_base_url TEXT NOT NULL, kafka_bootstrap TEXT,
                is_default INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
            INSERT INTO environment VALUES ('local','http://x','http://y','http://z',NULL,1,'2026-01-01');
            PRAGMA user_version = 3;
            """
        )
        conn.commit()

    store.list_environments()  # opening applies the migration

    with sqlite3.connect(isolated_store) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "service_account_credential" in tables
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4


def test_set_get_credential(isolated_store: Path) -> None:
    ident = _seed()
    store.set_service_account_credential(
        ident.id, "local", "globo", "shh",
        public_key_path="/k/pub", private_key_path="/k/priv", key_version=2,
    )
    cred = store.get_service_account_credential(ident.id)
    assert cred is not None
    assert cred.client_secret_plaintext == "shh"
    assert cred.org_name == "globo"
    assert cred.private_key_path == "/k/priv"
    assert cred.key_version == 2


def test_set_credential_upserts(isolated_store: Path) -> None:
    ident = _seed()
    store.set_service_account_credential(ident.id, "local", "globo", "one", key_version=1)
    store.set_service_account_credential(ident.id, "local", "globo", "two", key_version=1)
    assert store.get_service_account_credential(ident.id).client_secret_plaintext == "two"


def test_update_keys_only(isolated_store: Path) -> None:
    ident = _seed()
    store.set_service_account_credential(ident.id, "local", "globo", "shh", key_version=1)
    store.update_service_account_keys(ident.id, "/k/pub2", "/k/priv2", 2)
    cred = store.get_service_account_credential(ident.id)
    assert (cred.public_key_path, cred.private_key_path, cred.key_version) == ("/k/pub2", "/k/priv2", 2)
    assert cred.client_secret_plaintext == "shh"  # untouched


def test_credential_org_fk_enforced(isolated_store: Path) -> None:
    ident = _seed()
    with pytest.raises(sqlite3.IntegrityError):
        store.set_service_account_credential(ident.id, "local", "ghost-org", "shh")


def test_delete_identity_cascades_credential(isolated_store: Path) -> None:
    ident = _seed()
    store.set_service_account_credential(ident.id, "local", "globo", "shh")
    store.delete_identity("local", "sys")
    with sqlite3.connect(isolated_store) as conn:
        assert conn.execute("SELECT COUNT(*) FROM service_account_credential").fetchone()[0] == 0
