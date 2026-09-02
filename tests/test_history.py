"""Tests for `vl history` and the command-history ring buffer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vl.app import app
from vl.lib import store

runner = CliRunner()


def test_v6_store_migrates_to_v7(
    isolated_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "SCHEMA_VERSION", 6)
    store.ensure_local_environment_seeded()

    monkeypatch.setattr(store, "SCHEMA_VERSION", 7)
    store.list_environments()  # reopening applies 6 -> 7

    with sqlite3.connect(isolated_store) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "command_history" in tables


def test_history_shows_recent_commands_oldest_first() -> None:
    store.record_command(["env", "list"])
    store.record_command(["whoami"])
    store.record_command(["org", "show", "globo"])

    result = runner.invoke(app, ["history"], env={"VL_OUTPUT": "json"})

    assert result.exit_code == 0, result.stdout
    first = result.stdout.index("vl env list")
    middle = result.stdout.index("vl whoami")
    last = result.stdout.index("vl org show globo")
    assert first < middle < last


def test_history_limit_flag() -> None:
    for i in range(10):
        store.record_command(["whoami", f"--as=u{i}"])

    result = runner.invoke(app, ["history", "-n", "3"], env={"VL_OUTPUT": "json"})

    assert result.exit_code == 0, result.stdout
    assert result.stdout.count('"command"') == 3
    assert "u9" in result.stdout and "u7" in result.stdout and "u6" not in result.stdout


def test_history_redacts_credentials() -> None:
    store.record_command(["usr-acct", "cache", "--username", "admin", "--password", "hunter2"])
    store.record_command(["svc-acct", "cache", "--client-id=abc", "--secret=shh"])

    entries = store.list_command_history(25)

    joined = "\n".join(e.command for e in entries)
    assert "hunter2" not in joined
    assert "shh" not in joined
    assert "abc" not in joined
    assert "--password ***" in joined
    assert "--secret=***" in joined
    assert "--username admin" in joined  # non-secret args survive


def test_history_ring_buffer_trims_to_keep_limit() -> None:
    for i in range(store._HISTORY_KEEP + 25):
        store.record_command(["whoami", f"--as=u{i}"])

    assert len(store.list_command_history(10_000)) == store._HISTORY_KEEP


def test_record_command_never_raises_on_bad_store(monkeypatch) -> None:
    monkeypatch.setenv("VL_STORE_PATH", "/proc/nonexistent/cannot/write/store.db")
    store.record_command(["whoami"])  # must not raise


def test_history_empty() -> None:
    result = runner.invoke(app, ["history"])
    assert result.exit_code == 0
    assert "no results" in result.stdout.lower()


def test_history_hides_stale_history_rows_from_older_vl() -> None:
    # A store written by an older `vl` that still logged `vl history`.
    store.record_command(["whoami"])
    with store._store() as conn:
        conn.executemany(
            "INSERT INTO command_history (ran_at, argv) VALUES ('2026-01-01T00:00:00', ?)",
            [("vl history",), ("vl history -n 5",)],
        )
        conn.commit()

    commands = [e.command for e in store.list_command_history(25)]
    assert commands == ["vl whoami"]


def test_main_does_not_record_history_command(monkeypatch: pytest.MonkeyPatch) -> None:
    from vl import app as app_module

    store.record_command(["whoami"])
    monkeypatch.setattr(app_module.sys, "argv", ["vl", "history", "-n", "5"])
    monkeypatch.setattr(app_module, "app", lambda: None)
    app_module.main()

    commands = [e.command for e in store.list_command_history(25)]
    assert commands == ["vl whoami"]
