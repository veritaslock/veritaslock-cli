"""Tests for svc_acct + the v4 -> v5 table rename migration."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from vl.lib import store


def _tables(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def _seed() -> store.Identity:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    return store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "sys")


def test_fresh_store_uses_new_table_names(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    tables = _tables(isolated_store)
    assert {"user_acct", "svc_acct"} <= tables
    assert "user_credential" not in tables
    assert "service_account_credential" not in tables
    with sqlite3.connect(isolated_store) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION


def test_v4_store_migrates_to_v5_preserving_rows(
    isolated_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Build a genuine v4 store (old table names) using the store's own migrations.
    monkeypatch.setattr(store, "SCHEMA_VERSION", 4)
    sa_ident = _seed()
    user_ident = store.add_identity("local", "USER", "u-1", "u-1", "usr")
    ident = sa_ident
    with sqlite3.connect(isolated_store) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO service_account_credential "
            "(identity_id, environment_name, org_name, client_secret_plaintext, key_version) "
            "VALUES (?, 'local', 'globo', 'old-secret', 7)",
            (sa_ident.id,),
        )
        conn.execute(
            "INSERT INTO user_credential (identity_id, password_plaintext) VALUES (?, 'pw')",
            (user_ident.id,),
        )
        conn.commit()
    assert {"user_credential", "service_account_credential"} <= _tables(isolated_store)

    # Real version -> reopening runs the v4 -> v5 rename.
    monkeypatch.setattr(store, "SCHEMA_VERSION", 5)
    cred = store.get_svc_acct(ident.id)

    tables = _tables(isolated_store)
    assert {"user_acct", "svc_acct"} <= tables
    assert "service_account_credential" not in tables
    assert "user_credential" not in tables
    with sqlite3.connect(isolated_store) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    # rows survived the rename verbatim
    assert cred is not None
    assert cred.client_secret_plaintext == "old-secret"
    assert cred.key_version == 7
    assert store.get_user_acct(store.get_identity("local", "usr").id).password_plaintext == "pw"


def test_set_get_credential(isolated_store: Path) -> None:
    ident = _seed()
    store.set_svc_acct(
        ident.id, "local", "globo", "shh",
        public_key_path="/k/pub", private_key_path="/k/priv", key_version=2,
    )
    cred = store.get_svc_acct(ident.id)
    assert cred is not None
    assert cred.client_secret_plaintext == "shh"
    assert cred.org_name == "globo"
    assert cred.private_key_path == "/k/priv"
    assert cred.key_version == 2


def test_set_credential_upserts(isolated_store: Path) -> None:
    ident = _seed()
    store.set_svc_acct(ident.id, "local", "globo", "one", key_version=1)
    store.set_svc_acct(ident.id, "local", "globo", "two", key_version=1)
    assert store.get_svc_acct(ident.id).client_secret_plaintext == "two"


def test_update_keys_only(isolated_store: Path) -> None:
    ident = _seed()
    store.set_svc_acct(ident.id, "local", "globo", "shh", key_version=1)
    store.update_svc_acct_keys(ident.id, "/k/pub2", "/k/priv2", 2)
    cred = store.get_svc_acct(ident.id)
    assert (cred.public_key_path, cred.private_key_path, cred.key_version) == ("/k/pub2", "/k/priv2", 2)
    assert cred.client_secret_plaintext == "shh"  # untouched


def test_credential_org_fk_enforced(isolated_store: Path) -> None:
    ident = _seed()
    with pytest.raises(sqlite3.IntegrityError):
        store.set_svc_acct(ident.id, "local", "ghost-org", "shh")


def test_delete_identity_cascades_credential(isolated_store: Path) -> None:
    ident = _seed()
    store.set_svc_acct(ident.id, "local", "globo", "shh")
    store.delete_identity("local", "sys")
    with sqlite3.connect(isolated_store) as conn:
        assert conn.execute("SELECT COUNT(*) FROM svc_acct").fetchone()[0] == 0
