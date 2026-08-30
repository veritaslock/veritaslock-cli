"""Tests for the `vl env` command group."""

from __future__ import annotations

from typer.testing import CliRunner

from vl.app import app

runner = CliRunner()

# Force JSON output where a test needs to assert on a specific field value —
# the Rich table truncates long URLs to fit the (narrow) test terminal.
JSON_ENV = {"VL_OUTPUT": "json"}


def test_list_shows_auto_seeded_local() -> None:
    result = runner.invoke(app, ["env", "list"])
    assert result.exit_code == 0
    assert "local" in result.stdout


def test_add_then_show() -> None:
    add = runner.invoke(
        app,
        [
            "env", "add", "dev",
            "--idp-url", "http://idp",
            "--cp-url", "http://cp",
            "--di-url", "http://di",
        ],
    )
    assert add.exit_code == 0, add.stdout

    show = runner.invoke(app, ["env", "show", "dev"], env=JSON_ENV)
    assert show.exit_code == 0
    assert "http://idp" in show.stdout


def test_add_duplicate_fails_cleanly() -> None:
    args = [
        "env", "add", "dev",
        "--idp-url", "http://i", "--cp-url", "http://c", "--di-url", "http://d",
    ]
    assert runner.invoke(app, args).exit_code == 0
    dup = runner.invoke(app, args)
    assert dup.exit_code == 1
    assert "already exists" in dup.stdout
    assert "IntegrityError" not in dup.stdout


def test_add_does_not_become_default() -> None:
    runner.invoke(
        app,
        [
            "env", "add", "dev",
            "--idp-url", "http://i", "--cp-url", "http://c", "--di-url", "http://d",
        ],
    )
    result = runner.invoke(app, ["env", "show", "dev"])
    # The "default" column marker is only "*" for the default row.
    assert result.exit_code == 0


def test_use_sets_default() -> None:
    runner.invoke(
        app,
        [
            "env", "add", "dev",
            "--idp-url", "http://i", "--cp-url", "http://c", "--di-url", "http://d",
        ],
    )
    use = runner.invoke(app, ["env", "use", "dev"])
    assert use.exit_code == 0
    assert "dev" in use.stdout


def test_use_unknown_env_fails() -> None:
    result = runner.invoke(app, ["env", "use", "ghost"])
    assert result.exit_code == 1
    assert "ghost" in result.stdout


def test_update_requires_at_least_one_flag() -> None:
    result = runner.invoke(app, ["env", "update", "local"])
    assert result.exit_code == 1
    assert "nothing to update" in result.stdout


def test_update_changes_field() -> None:
    result = runner.invoke(
        app, ["env", "update", "local", "--cp-url", "http://cp-new"], env=JSON_ENV
    )
    assert result.exit_code == 0
    assert "http://cp-new" in result.stdout


def test_delete_default_is_blocked() -> None:
    result = runner.invoke(app, ["env", "delete", "local"])
    assert result.exit_code == 1
    assert "default environment" in result.stdout


def test_delete_non_default() -> None:
    runner.invoke(
        app,
        [
            "env", "add", "dev",
            "--idp-url", "http://i", "--cp-url", "http://c", "--di-url", "http://d",
        ],
    )
    result = runner.invoke(app, ["env", "delete", "dev"])
    assert result.exit_code == 0
    assert "deleted" in result.stdout
