"""Tests for the identity / credential / membership / token-cache store (Phase 3)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from vl.lib import store


def _seed_org(env: str = "local", name: str = "globo") -> None:
    store.ensure_local_environment_seeded()
    store.upsert_organization(env, name, f"srv-{name}", name.title(), active=True)


def _mk_identity(label: str = "alice", *, env: str = "local", server_id: str = "u-1") -> store.Identity:
    return store.add_identity(env, "USER", server_id, "alice", label)


# --------------------------------------------------------------------------- #
# migration
# --------------------------------------------------------------------------- #


def test_v3_tables_exist(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    with sqlite3.connect(isolated_store) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"identity", "user_acct", "org_membership", "token_cache"} <= tables


# --------------------------------------------------------------------------- #
# identity CRUD + default
# --------------------------------------------------------------------------- #


def test_add_and_get_identity(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    ident = _mk_identity()
    assert ident.kind == "USER"
    assert ident.is_default is False
    assert store.get_identity("local", "alice").server_id == "u-1"


def test_duplicate_label_rejected(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    _mk_identity("alice")
    with pytest.raises(store.IdentityExistsError):
        _mk_identity("alice", server_id="u-2")


def test_get_missing_identity_raises(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    with pytest.raises(store.IdentityNotFoundError):
        store.get_identity("local", "nope")


def test_lookup_by_server_id_and_principal(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    _mk_identity("alice", server_id="u-1")
    assert store.get_identity_by_server_id("local", "u-1", "USER").label == "alice"
    assert store.get_identity_by_server_id("local", "u-x", "USER") is None
    assert store.get_identity_by_principal("local", "alice", "USER").label == "alice"


def test_use_keeps_single_default_per_environment(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    _mk_identity("alice", server_id="u-1")
    _mk_identity("bob", server_id="u-2")

    store.set_default_identity("local", "alice")
    store.set_default_identity("local", "bob")

    defaults = [i.label for i in store.list_identities("local") if i.is_default]
    assert defaults == ["bob"]


def test_list_identities_filters_by_kind(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    _mk_identity("alice")
    assert len(store.list_identities("local", kind="USER")) == 1
    assert store.list_identities("local", kind="SERVICE_ACCOUNT") == []


# --------------------------------------------------------------------------- #
# resolve_identity precedence (§8.3)
# --------------------------------------------------------------------------- #


def test_resolve_prefers_explicit_label(isolated_store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store.ensure_local_environment_seeded()
    _mk_identity("alice", server_id="u-1")
    _mk_identity("bob", server_id="u-2")
    store.set_default_identity("local", "alice")
    monkeypatch.setenv("VL_IDENTITY", "alice")

    assert store.resolve_identity("local", "bob").label == "bob"


def test_resolve_falls_back_to_env_var(isolated_store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store.ensure_local_environment_seeded()
    _mk_identity("alice", server_id="u-1")
    _mk_identity("bob", server_id="u-2")
    store.set_default_identity("local", "alice")
    monkeypatch.setenv("VL_IDENTITY", "bob")

    assert store.resolve_identity("local", None).label == "bob"


def test_resolve_falls_back_to_default(isolated_store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VL_IDENTITY", raising=False)
    store.ensure_local_environment_seeded()
    _mk_identity("alice", server_id="u-1")
    store.set_default_identity("local", "alice")

    assert store.resolve_identity("local", None).label == "alice"


def test_resolve_unresolvable_raises(isolated_store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VL_IDENTITY", raising=False)
    store.ensure_local_environment_seeded()
    with pytest.raises(store.NoResolvedIdentityError):
        store.resolve_identity("local", None)


# --------------------------------------------------------------------------- #
# credentials / memberships / cascade
# --------------------------------------------------------------------------- #


def test_user_acct_tiers(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    ident = _mk_identity()

    store.set_user_acct(ident.id, "s3cret")
    assert store.get_user_acct(ident.id).password_plaintext == "s3cret"

    store.set_user_acct(ident.id, None)  # tier 2
    assert store.get_user_acct(ident.id).password_plaintext is None


def test_org_membership_requires_cached_org(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    ident = _mk_identity()
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_org_membership(ident.id, "local", "ghost", "USER")


def test_org_membership_upsert_and_list(isolated_store: Path) -> None:
    _seed_org()
    ident = _mk_identity()
    store.upsert_org_membership(ident.id, "local", "globo", "USER")
    store.upsert_org_membership(ident.id, "local", "globo", "ORG_ADMIN")  # role change

    memberships = store.list_org_memberships(ident.id)
    assert len(memberships) == 1
    assert memberships[0].role == "ORG_ADMIN"


def test_delete_identity_cascades(isolated_store: Path) -> None:
    _seed_org()
    ident = _mk_identity()
    store.set_user_acct(ident.id, "s3cret")
    store.upsert_org_membership(ident.id, "local", "globo", "USER")
    now = datetime.now(timezone.utc)
    store.set_cached_token(ident.id, "tok", now, now + timedelta(hours=1))

    store.delete_identity("local", "alice")

    with sqlite3.connect(isolated_store) as conn:
        for table in ("user_acct", "org_membership", "token_cache"):
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0, table


# --------------------------------------------------------------------------- #
# token cache
# --------------------------------------------------------------------------- #


def test_token_cache_hit_and_expiry(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    ident = _mk_identity()
    now = datetime.now(timezone.utc)

    store.set_cached_token(ident.id, "tok", now, now + timedelta(hours=1))
    assert store.get_cached_token(ident.id).token == "tok"

    store.set_cached_token(ident.id, "old", now - timedelta(hours=2), now - timedelta(hours=1))
    assert store.get_cached_token(ident.id) is None  # expired

    store.clear_cached_token(ident.id)
    assert store.get_cached_token(ident.id) is None


def test_token_cache_skew_margin(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    ident = _mk_identity()
    now = datetime.now(timezone.utc)
    # expires in 10s — inside the 30s skew window, so treated as already expired
    store.set_cached_token(ident.id, "tok", now, now + timedelta(seconds=10))
    assert store.get_cached_token(ident.id) is None
