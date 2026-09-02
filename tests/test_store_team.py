"""Tests for the team / team_member cache + the v5 -> v6 migration (Phase 6)."""

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


def _seed_org(env: str = "local", name: str = "globo") -> None:
    store.ensure_local_environment_seeded()
    store.upsert_organization(env, name, f"srv-{name}", name.title(), active=True)


# --------------------------------------------------------------------------- #
# migration
# --------------------------------------------------------------------------- #


def test_v6_tables_exist(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    tables = _tables(isolated_store)
    assert {"team", "team_member"} <= tables
    with sqlite3.connect(isolated_store) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION


def test_v5_store_migrates_to_v6(isolated_store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "SCHEMA_VERSION", 5)
    store.ensure_local_environment_seeded()
    assert "team" not in _tables(isolated_store)

    monkeypatch.setattr(store, "SCHEMA_VERSION", 6)
    store.list_environments()  # reopening applies 5 -> 6
    assert {"team", "team_member"} <= _tables(isolated_store)
    with sqlite3.connect(isolated_store) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6


# --------------------------------------------------------------------------- #
# team cache
# --------------------------------------------------------------------------- #


def test_upsert_and_get_team(isolated_store: Path) -> None:
    _seed_org()
    team = store.upsert_team(
        "local", "globo", "ingest", "t-1", description="Ingest team", created_by="u-1"
    )
    assert team.server_team_id == "t-1"
    assert store.get_team("local", "globo", "ingest").description == "Ingest team"


def test_upsert_team_is_idempotent_and_refreshes(isolated_store: Path) -> None:
    _seed_org()
    first = store.upsert_team("local", "globo", "ingest", "t-1", description="old")
    second = store.upsert_team("local", "globo", "ingest", "t-1", description="new")
    assert second.description == "new"
    assert second.synced_at >= first.synced_at
    assert len(store.list_teams("local", "globo")) == 1


def test_team_requires_cached_org(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_team("local", "ghost", "ingest", "t-1")


def test_get_missing_team_raises(isolated_store: Path) -> None:
    _seed_org()
    with pytest.raises(store.TeamNotFoundError):
        store.get_team("local", "globo", "nope")
    assert store.get_team_or_none("local", "globo", "nope") is None


def test_list_teams_scoped(isolated_store: Path) -> None:
    _seed_org(name="globo")
    _seed_org(name="acme")
    store.upsert_team("local", "globo", "a", "t-a")
    store.upsert_team("local", "acme", "b", "t-b")
    assert [t.name for t in store.list_teams("local", "globo")] == ["a"]
    assert len(store.list_teams("local")) == 2


# --------------------------------------------------------------------------- #
# team_member
# --------------------------------------------------------------------------- #


def _member(label: str = "alice", server_id: str = "u-1") -> store.Identity:
    return store.add_identity("local", "USER", server_id, label, label)


def test_team_member_upsert_list_delete(isolated_store: Path) -> None:
    _seed_org()
    store.upsert_team("local", "globo", "ingest", "t-1")
    ident = _member()

    store.upsert_team_member(ident.id, "local", "globo", "ingest", "TEAM_MEMBER")
    store.upsert_team_member(ident.id, "local", "globo", "ingest", "TEAM_ADMIN")  # role change
    memberships = store.list_team_memberships(ident.id)
    assert len(memberships) == 1 and memberships[0].role == "TEAM_ADMIN"

    store.delete_team_member(ident.id, "local", "globo", "ingest")
    assert store.list_team_memberships(ident.id) == []


def test_team_member_requires_cached_team(isolated_store: Path) -> None:
    _seed_org()
    ident = _member()
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_team_member(ident.id, "local", "globo", "no-such-team", "TEAM_MEMBER")


def test_deleting_team_cascades_members(isolated_store: Path) -> None:
    _seed_org()
    store.upsert_team("local", "globo", "ingest", "t-1")
    ident = _member()
    store.upsert_team_member(ident.id, "local", "globo", "ingest", "TEAM_ADMIN")

    store.delete_team("local", "globo", "ingest")
    assert store.list_team_memberships(ident.id) == []
    with sqlite3.connect(isolated_store) as conn:
        assert conn.execute("SELECT COUNT(*) FROM team_member").fetchone()[0] == 0


def test_deleting_identity_cascades_team_members(isolated_store: Path) -> None:
    _seed_org()
    store.upsert_team("local", "globo", "ingest", "t-1")
    ident = _member()
    store.upsert_team_member(ident.id, "local", "globo", "ingest", "TEAM_ADMIN")

    store.delete_identity("local", "alice")
    with sqlite3.connect(isolated_store) as conn:
        assert conn.execute("SELECT COUNT(*) FROM team_member").fetchone()[0] == 0


def test_delete_missing_team_raises(isolated_store: Path) -> None:
    _seed_org()
    with pytest.raises(store.TeamNotFoundError):
        store.delete_team("local", "globo", "nope")
