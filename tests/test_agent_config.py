"""The agent's command line and ``agent.properties`` (vl-agent-spec.md §3)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from vl.agent import EXIT_CONFIG, AgentExit
from vl.agent import config

LOG = logging.getLogger("test.agent.config")


def test_defaults_without_a_file(tmp_path: Path) -> None:
    s = config.load_settings(tmp_path / "agent.properties", LOG)
    assert s == config.Settings()
    assert (s.heartbeat_interval_seconds, s.host_check_interval_seconds, s.skew_warn_seconds) == (10, 60, 5)
    assert (s.results_all_orgs, s.results_max_bytes, s.results_keep) == (False, 10_485_760, 5)


def test_every_setting_can_be_overridden(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "agent.properties"
    path.write_text(
        "# comment\n! another\n\n"
        "agent.heartbeat.interval-seconds = 2.5\n"
        "agent.heartbeat.stale-seconds=45\n"
        "agent.host.check-interval-seconds=15\n"
        "agent.command.skew-warn-seconds=1\n"
        "agent.results.all-orgs=TRUE\n"
        "agent.results.max-bytes=1000\n"
        "agent.results.keep=2\n"
        "agent.topics.commands=cmds\n"
        "agent.topics.results=res\n"
        "agent.no-such=1\n"
    )
    with caplog.at_level(logging.WARNING, logger="test.agent.config"):
        s = config.load_settings(path, LOG)
    assert s == config.Settings(2.5, 45, 15, 1, True, 1000, 2, "cmds", "res")
    assert any("agent.no-such" in r.message for r in caplog.records)


@pytest.mark.parametrize("line", [
    "agent.heartbeat.interval-seconds=fast", "agent.heartbeat.interval-seconds=0",
    "agent.results.keep=0", "agent.results.max-bytes=1.5", "agent.results.all-orgs=yes",
    "agent.topics.results=", "no equals sign",
])
def test_a_bad_value_is_a_configuration_error(tmp_path: Path, line: str) -> None:
    path = tmp_path / "agent.properties"
    path.write_text(line + "\n")
    with pytest.raises(AgentExit) as e:
        config.load_settings(path, LOG)
    assert e.value.code == EXIT_CONFIG and ":1:" in e.value.message


def test_command_line() -> None:
    a = config.parse_args(["--as", "globo-agent", "--env", "optiplex", "--host", "10.0.0.1",
                           "--verilock", "/opt/v", "--name", "lab-2", "--log-level", "debug"])
    assert a == config.Args("globo-agent", "optiplex", "10.0.0.1", Path("/opt/v"), "lab-2", "DEBUG")


@pytest.mark.parametrize("argv", [[], ["--as"], ["--as", "x", "--host", " "], ["--as", "x", "--bogus"]])
def test_a_bad_command_line_exits_78(argv: list[str]) -> None:
    with pytest.raises(AgentExit) as e:
        config.parse_args(argv)
    assert e.value.code == EXIT_CONFIG


def test_paths_are_under_home(isolated_home: Path) -> None:
    assert config.results_path("globo") == isolated_home / "orgs/globo/agent/data/results.log"
    assert config.default_verilock() == isolated_home / ".veritaslock/bin/verilock"


@pytest.mark.parametrize("name, ok", [("dev-box", True), ("a.b_c-1", True), ("x" * 64, True),
                                      ("x" * 65, False), ("dev box", False), ("", False)])
def test_name_pattern_matches_the_control_planes(name: str, ok: bool) -> None:
    assert bool(config.NAME_PATTERN.match(name)) is ok
