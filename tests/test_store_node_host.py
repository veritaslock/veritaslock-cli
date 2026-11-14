"""The configured node host (store migration 10 -> 11): a nullable
``environment.node_host``, and nothing else changed."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from test_store_settings import _dump, _v9_store
from vl.lib import store


def _v10_store(path: Path) -> None:
    """A version-10 store with an env, orgs, nodes, history, an agent and an issued command."""
    _v9_store(path, [("globo", 1, 7001), ("acme", 1, 7002)])
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(store._MIGRATIONS[9])
    conn.execute("PRAGMA user_version = 10")
    sa = conn.execute("SELECT identity_id FROM svc_acct ORDER BY identity_id").fetchone()[0]
    conn.execute("INSERT INTO agent (environment_name, org_name, svc_acct_id, pid, state, created_at) "
                 "VALUES ('local', 'globo', ?, 4242, 'RUNNING', 't0')", (sa,))
    conn.execute("INSERT INTO issued_command (environment_name, correlation_id, command_type, scope, "
                 "org_name, org_node, issued_at, expires_at) VALUES ('local', 'c-1', 'START', 'ORG', "
                 "'globo', NULL, 't1', 't2')")
    conn.commit()
    conn.close()


def test_migration_from_v10_adds_only_a_null_node_host(isolated_store: Path) -> None:
    _v10_store(isolated_store)
    before = _dump(isolated_store)

    env = store.get_environment("local")

    after = _dump(isolated_store)
    assert before["user_version"] == 10 and after["user_version"] == store.SCHEMA_VERSION == 11
    assert env.node_host is None
    assert set(after["rows"]) == set(before["rows"])  # no table added or removed
    for table, rows in before["rows"].items():
        expected = [(*row, None) for row in rows] if table == "environment" else rows
        assert after["rows"][table] == expected, table
    changed = sorted(set(after["schema"]) ^ set(before["schema"]))
    assert len(changed) == 2 and all("CREATE TABLE environment" in sql for sql in changed)
    assert [sql for sql in after["schema"] if "CREATE TABLE environment" in sql][0].rstrip().endswith(
        "node_host TEXT)")


def test_a_new_store_has_the_column_and_the_seed_has_no_host() -> None:
    assert store.get_environment("local").node_host is None


def test_add_update_and_clear_the_node_host() -> None:
    store.ensure_local_environment_seeded()
    env = store.add_environment("lab", "http://i", "http://c", "http://d", "http://t", "http://s", "k:9092",
                                node_host="192.168.8.2")
    assert env.node_host == "192.168.8.2" and store.get_environment("lab").node_host == "192.168.8.2"
    assert store.update_environment("lab", kafka_bootstrap="k2:9092").node_host == "192.168.8.2"
    assert store.update_environment("lab", node_host="10.0.0.5").node_host == "10.0.0.5"
    cleared = store.update_environment("lab", clear_node_host=True)
    assert cleared.node_host is None and cleared.kafka_bootstrap == "k2:9092"
    with pytest.raises(ValueError):
        store.update_environment("lab", node_host="x", clear_node_host=True)
