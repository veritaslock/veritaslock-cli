"""Store settings and the v10 migration (vl-agent-cli-spec.md §3, §5.2).

WAL and a 10-second busy timeout on every connection, BEGIN IMMEDIATE for the
read-then-write transactions, a unique node port, the agent and issued_command
tables. Every test runs against its own temporary store (conftest.py).
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
import respx
from typer.testing import CliRunner

from _helpers import add_user
from vl.app import app
from vl.lib import store

runner = CliRunner()
SRC = Path(__file__).resolve().parent.parent / "src" / "vl"


# --------------------------------------------------------------------------- #
# helpers: a store at schema version 9, written the way an older `vl` wrote it
# --------------------------------------------------------------------------- #


def _v9_store(path: Path, ports: list[tuple[str, int, int]]) -> None:
    """A store at version 9 with an env, two orgs, history, and one node per
    ``(org, org_node, port)``, written with plain sqlite3 (no WAL, no v10)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    for i in range(9):
        conn.executescript(store._MIGRATIONS[i])
    conn.execute("PRAGMA user_version = 9")
    conn.execute(
        "INSERT INTO environment (name, idp_base_url, cp_base_url, di_base_url, token_url, "
        "schema_reg_url, kafka_bootstrap, is_default, created_at) "
        "VALUES ('local', 'http://i', 'http://c', 'http://d', 'http://t', 'http://s', 'k:9092', 1, 't0')"
    )
    for org, org_id in (("globo", "org-1"), ("acme", "org-2")):
        conn.execute(
            "INSERT INTO organization VALUES ('local', ?, ?, ?, 1, 't0', 't0')",
            (org, org_id, org.title()),
        )
    conn.execute("INSERT INTO command_history (ran_at, argv) VALUES ('t1', 'vl node list')")
    for org, n, port in ports:
        cur = conn.execute(
            "INSERT INTO identity (environment_name, kind, server_id, principal_name, label, created_at) "
            "VALUES ('local', 'SERVICE_ACCOUNT', ?, ?, ?, 't0')",
            (f"sa-{org}-{n}", f"{org}-{n}", f"{org}-{n}"),
        )
        conn.execute(
            "INSERT INTO svc_acct (identity_id, environment_name, org_name, client_id, "
            "client_secret_plaintext, role) VALUES (?, 'local', ?, ?, 'secret', 'NODE')",
            (cur.lastrowid, org, f"client-{org}-{n}"),
        )
        conn.execute(
            "INSERT INTO node (environment_name, org_name, org_node, svc_acct_id, port, pid, state, created_at) "
            "VALUES ('local', ?, ?, ?, ?, NULL, 'STOPPED', 't0')",
            (org, n, cur.lastrowid, port),
        )
    conn.commit()
    conn.close()


def _dump(path: Path) -> dict[str, Any]:
    """Every table's rows, the schema, user_version and journal mode, read raw."""
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        return {
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
            "schema": sorted(r[0] for r in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")),
            "rows": {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables},
        }
    finally:
        conn.close()


def _columns(path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# the migration 9 -> 10
# --------------------------------------------------------------------------- #


def test_migration_from_v9_keeps_every_row_and_adds_the_v10_schema(isolated_store: Path) -> None:
    _v9_store(isolated_store, [("globo", 1, 7001), ("globo", 2, 7002), ("acme", 1, 7003)])
    before = _dump(isolated_store)

    nodes = store.get_nodes(store.get_environment("local"))

    after = _dump(isolated_store)
    assert after["user_version"] == store.SCHEMA_VERSION == 10
    assert after["journal_mode"] == "wal"
    for table, rows in before["rows"].items():
        assert after["rows"][table] == rows, table  # nothing lost or changed
    assert [(n.org_name, n.org_node, n.port) for n in nodes] == [
        ("acme", 1, 7003), ("globo", 1, 7001), ("globo", 2, 7002)
    ]
    assert _columns(isolated_store, "agent") == [
        "id", "environment_name", "org_name", "svc_acct_id", "pid", "state", "created_at"
    ]
    assert _columns(isolated_store, "issued_command") == [
        "id", "environment_name", "correlation_id", "command_type", "scope",
        "org_name", "org_node", "issued_at", "expires_at",
    ]
    # vl history's table is untouched: same columns, same row
    assert _columns(isolated_store, "command_history") == ["id", "ran_at", "argv"]
    assert [e.command for e in store.list_command_history(10)] == ["vl node list"]


def test_migration_refuses_nodes_that_share_a_port_and_changes_nothing(isolated_store: Path) -> None:
    _v9_store(isolated_store, [("globo", 1, 7001), ("acme", 1, 7001), ("globo", 2, 7002),
                               ("acme", 2, 7005), ("globo", 3, 7005)])
    before = _dump(isolated_store)

    with pytest.raises(store.DuplicatePortsError) as refused:
        store.get_environment("local")

    message = str(refused.value)
    assert "can't be upgraded to schema version 10" in message
    assert "port 7001: globo/node1, acme/node1" in message
    assert "port 7005: acme/node2, globo/node3" in message
    assert "7002" not in message
    assert "Nothing was changed." in message and str(isolated_store) in message
    assert _dump(isolated_store) == before  # same rows, schema, user_version 9, no WAL


def test_a_refused_migration_is_one_clean_error_from_any_command(isolated_store: Path) -> None:
    _v9_store(isolated_store, [("globo", 1, 7001), ("acme", 1, 7001)])

    result = runner.invoke(app, ["env", "list"], env={"COLUMNS": "200"})

    assert result.exit_code == 1
    assert "port 7001: globo/node1, acme/node1" in result.output
    assert _dump(isolated_store)["user_version"] == 9


# --------------------------------------------------------------------------- #
# WAL and the busy timeout on every connection
# --------------------------------------------------------------------------- #


class _Seen:
    settings: list[tuple[str, int, int]] = []


class _RecordingConnection(sqlite3.Connection):
    """Records each connection's journal mode, busy timeout and foreign keys as it closes."""

    def close(self) -> None:
        _Seen.settings.append((
            self.execute("PRAGMA journal_mode").fetchone()[0],
            self.execute("PRAGMA busy_timeout").fetchone()[0],
            self.execute("PRAGMA foreign_keys").fetchone()[0],
        ))
        super().close()


def test_every_connection_is_wal_with_a_10_second_busy_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = sqlite3.connect
    _Seen.settings = []
    monkeypatch.setattr(
        store.sqlite3, "connect",
        lambda *a, **kw: real_connect(*a, factory=_RecordingConnection, **kw),
    )

    # Reads, writes, read-then-writes and the first open of a new store.
    store.ensure_local_environment_seeded()
    env = store.get_environment("local")
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "t0")
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "globo-1", "globo-1")
    store.set_svc_acct(ident.id, "local", "globo", "client-1", "secret", role="NODE")
    node = store.add_node("local", "globo", 1, ident.id)
    store.set_node_stopped(store.set_pid(node, 99))
    store.get_nodes(env)
    store.record_command(["node", "list"])
    store.record_issued_command("local", "c-1", "STOP", "ORG", "globo", None, "t1", "t2")

    assert len(_Seen.settings) >= 10
    assert set(_Seen.settings) == {("wal", store.BUSY_TIMEOUT_MS, 1)}
    assert store.BUSY_TIMEOUT_MS == 10_000


def test_the_store_is_the_only_place_that_opens_a_connection() -> None:
    """So the settings above can't be bypassed by a second, bare sqlite3.connect."""
    calls = [
        (path.relative_to(SRC), n)
        for path in SRC.rglob("*.py")
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"\bsqlite3\.connect\(", line)
    ]
    assert len(calls) == 1 and calls[0][0] == Path("lib/store.py")
    source = (SRC / "lib" / "store.py").read_text()
    connect_body = source[source.index("def _connect("):source.index("def _immediate(")]
    assert "sqlite3.connect(" in connect_body


def test_the_wal_files_are_owner_only_like_the_store(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    with store._store() as conn:  # an open connection keeps -wal and -shm in place
        conn.execute("INSERT INTO command_history (ran_at, argv) VALUES ('t', 'vl x')")
        conn.commit()
        for suffix in ("", "-wal", "-shm"):
            mode = stat.S_IMODE(os.stat(f"{isolated_store}{suffix}").st_mode)
            assert mode == 0o600, (suffix, oct(mode))


# --------------------------------------------------------------------------- #
# BEGIN IMMEDIATE
# --------------------------------------------------------------------------- #


def test_immediate_takes_the_write_lock_before_the_first_read(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    with store._store(immediate=True) as conn:
        assert conn.in_transaction
        other = sqlite3.connect(isolated_store, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        finally:
            other.close()


def test_immediate_rolls_back_on_an_error(isolated_store: Path) -> None:
    store.ensure_local_environment_seeded()
    with pytest.raises(RuntimeError):
        with store._store(immediate=True) as conn:
            conn.execute("INSERT INTO command_history (ran_at, argv) VALUES ('t', 'vl gone')")
            raise RuntimeError("boom")
    assert all(e.command != "vl gone" for e in store.list_command_history(10))


def _node_row(org: str = "globo", n: int = 1, port: int | None = None) -> store.Node:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "t0")
    ident = store.add_identity("local", "SERVICE_ACCOUNT", f"sa-{org}-{n}", f"{org}-{n}", f"{org}-{n}")
    store.set_svc_acct(ident.id, "local", org, f"client-{org}-{n}", "secret", role="NODE")
    return store.add_node("local", org, n, ident.id, port)


def test_the_repair_never_clobbers_a_row_that_changed_since_it_was_read() -> None:
    stale = store.set_pid(_node_row(), 100)  # what the repair read: RUNNING, pid 100
    store.set_pid(stale, 200)  # a start wrote a new pid since

    after = store.set_node_stopped_if_unchanged(stale)

    assert (after.node_state, after.pid) == ("RUNNING", 200)
    current = store.get_node("local", "globo", 1)
    assert (current.node_state, current.pid) == ("RUNNING", 200)
    repaired = store.set_node_stopped_if_unchanged(current)
    assert (repaired.node_state, repaired.pid) == ("STOPPED", None)


# --------------------------------------------------------------------------- #
# the unique port
# --------------------------------------------------------------------------- #


def test_ports_are_allocated_across_the_store_from_7001() -> None:
    first = _node_row("globo", 1)
    second = _node_row("globo", 2)
    assert (first.port, second.port) == (7001, 7002)


def test_an_explicit_port_already_in_use_is_refused_by_the_store(isolated_store: Path) -> None:
    _node_row("globo", 1, port=7010)
    with pytest.raises(store.PortInUseError, match="Port 7010 is already used by globo/node1"):
        _node_row("globo", 2, port=7010)
    # and the database itself refuses a duplicate written around the store's check
    conn = sqlite3.connect(isolated_store)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed: node.port"):
            conn.execute(
                "INSERT INTO node (environment_name, org_name, org_node, svc_acct_id, port, state, created_at) "
                "SELECT environment_name, org_name, 9, svc_acct_id, 7010, 'NEW', 't' FROM node"
            )
    finally:
        conn.close()


@respx.mock
def test_vl_node_create_refuses_a_used_port_before_it_creates_anything(isolated_home: Path) -> None:
    _node_row("globo", 1, port=7010)
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])
    # respx.mock with no routes: any HTTP call (the org lookup, the IdP) would fail the test

    result = runner.invoke(app, ["node", "create", "globo", "--port", "7010"], env={"COLUMNS": "200"})

    assert result.exit_code == 1
    assert "Port 7010 is already used by globo/node1" in result.output
    assert len(respx.calls) == 0
    assert [n.org_node for n in store.get_nodes(store.get_environment("local"))] == [1]
    assert not (isolated_home / "orgs").exists()


# Each child creates one node with an allocated port. The allocation is slowed
# down after its read, so without BEGIN IMMEDIATE the processes would read the
# same MAX(port) and collide.
_CREATOR = textwrap.dedent(
    """
    import sys, time
    from vl.lib import store

    n, start_at = int(sys.argv[1]), float(sys.argv[2])
    real = store._allocate_port

    def slow(conn):
        port = real(conn)
        time.sleep(0.2)
        return port

    store._allocate_port = slow
    ident = store.get_identity("local", f"globo-{n}")
    while time.time() < start_at:
        time.sleep(0.005)
    print(store.add_node("local", "globo", n, ident.id).port)
    """
)


def test_concurrent_creates_in_separate_processes_never_share_a_port(
    isolated_store: Path, isolated_home: Path
) -> None:
    count = 6
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "t0")
    for n in range(1, count + 1):
        ident = store.add_identity("local", "SERVICE_ACCOUNT", f"sa-{n}", f"globo-{n}", f"globo-{n}")
        store.set_svc_acct(ident.id, "local", "globo", f"client-{n}", "secret", role="NODE")
    env = {**os.environ, "VL_STORE_PATH": str(isolated_store), "HOME": str(isolated_home)}
    start_at = time.time() + 1.5

    children = [
        subprocess.Popen(
            [sys.executable, "-c", _CREATOR, str(n), str(start_at)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for n in range(1, count + 1)
    ]
    results = [child.communicate(timeout=60) for child in children]

    assert all(child.returncode == 0 for child in children), [err for _, err in results]
    ports = sorted(int(out.split()[-1]) for out, _ in results)
    assert ports == list(range(7001, 7001 + count))


# --------------------------------------------------------------------------- #
# the new tables
# --------------------------------------------------------------------------- #


def test_agent_is_one_per_org_and_environment_and_keeps_its_service_account(isolated_store: Path) -> None:
    node = _node_row()
    conn = sqlite3.connect(isolated_store)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute(
            "INSERT INTO agent (environment_name, org_name, svc_acct_id, created_at) "
            "VALUES ('local', 'globo', ?, 't')", (node.svc_acct_id,)
        )
        assert conn.execute("SELECT state, pid FROM agent").fetchone() == ("NEW", None)
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(
                "INSERT INTO agent (environment_name, org_name, svc_acct_id, created_at) "
                "VALUES ('local', 'globo', ?, 't')", (node.svc_acct_id,)
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute("UPDATE agent SET state = 'UP'")
        conn.execute("DELETE FROM node")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.execute("DELETE FROM svc_acct")
    finally:
        conn.close()


def test_issued_commands_keep_the_newest_50_per_environment() -> None:
    store.ensure_local_environment_seeded()
    store.add_environment("staging", "http://i", "http://c", "http://d", "http://t", "http://s", "k:9092")
    store.record_issued_command("staging", "s-1", "START", "NETWORK", None, None, "t", "t")
    for i in range(1, 56):
        store.record_issued_command("local", f"c-{i}", "STOP", "NODE", "globo", 3, f"t{i}", f"e{i}")

    local = store.list_issued_commands("local")
    assert len(local) == 50
    assert [c.correlation_id for c in (local[0], local[-1])] == ["c-55", "c-6"]  # newest first
    assert (local[0].scope, local[0].org_name, local[0].org_node) == ("NODE", "globo", 3)
    assert [c.correlation_id for c in store.list_issued_commands("staging")] == ["s-1"]
