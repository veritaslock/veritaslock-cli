"""Tests for HelpOnErrorGroup (vl/lib/cli.py): how `vl` answers a wrong invocation."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from vl.app import app

runner = CliRunner()


@pytest.mark.parametrize(
    ("argv", "usage", "error"),
    [
        # unknown option on a command
        (["study", "delete", "globo", "s-1", "--force"], "Usage: vl study delete", "No such option: --force"),
        # unknown option on a group, top level and nested
        (["--bogus"], "Usage: vl [OPTIONS] COMMAND", "No such option: --bogus"),
        (["study", "--bogus"], "Usage: vl study [OPTIONS] COMMAND", "No such option: --bogus"),
        # unknown command, top level and nested
        (["nope"], "Usage: vl [OPTIONS] COMMAND", "No such command 'nope'."),
        (["study", "blah"], "Usage: vl study [OPTIONS] COMMAND", "No such command 'blah'."),
        # missing argument, missing required option
        (["study", "delete", "globo"], "Usage: vl study delete", "Missing argument 'study_id'."),
        (["usr-acct", "add", "jdoe", "--org", "globo"], "Usage: vl usr-acct add", "Missing option '--email'."),
    ],
)
def test_a_usage_error_prints_the_error_then_the_help_and_exits_2(
    argv: list[str], usage: str, error: str
) -> None:
    result = runner.invoke(app, argv)

    assert result.exit_code == 2
    # one line; click may add a suggestion ("Did you mean 'node'?")
    assert result.stderr.startswith(f"Error: {error}")
    assert len(result.stderr.splitlines()) == 1
    assert usage in result.stdout
    assert "Error" not in result.stdout


@pytest.mark.parametrize(("argv", "usage"), [([], "Usage: vl [OPTIONS]"), (["study"], "Usage: vl study [OPTIONS]")])
def test_a_bare_group_prints_its_help_once_and_exits_0(argv: list[str], usage: str) -> None:
    result = runner.invoke(app, argv)

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.count(usage) == 1


@pytest.mark.parametrize(
    ("argv", "usage"),
    [
        (["--help"], "Usage: vl [OPTIONS]"),
        (["study", "--help"], "Usage: vl study [OPTIONS]"),
        (["study", "delete", "--help"], "Usage: vl study delete"),
    ],
)
def test_help_exits_0(argv: list[str], usage: str) -> None:
    result = runner.invoke(app, argv)

    assert result.exit_code == 0
    assert result.stderr == ""
    assert usage in result.stdout


def test_a_bad_parameter_keeps_clicks_own_error() -> None:
    result = runner.invoke(app, ["study", "add", "globo", "/no/such/study.json"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Try 'vl study add --help' for help." in result.stderr
    assert "does not exist" in result.stderr
