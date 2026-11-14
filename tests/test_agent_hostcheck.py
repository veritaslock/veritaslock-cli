"""The agent's host: ``verilock --print-host``, ``--host``, and the drift check
(vl-agent-spec.md §3, §8 items 2 and 7). Every binary here is a stand-in script."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from vl.agent import EXIT_CONFIG, EXIT_HOST_CHANGED, EXIT_OK, AgentExit
from vl.agent import hostcheck
from vl.agent.runner import Stopper, host_check_once


def script(tmp_path: Path, body: str, name: str = "verilock") -> Path:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def test_print_host_returns_the_one_line(tmp_path: Path) -> None:
    binary = script(tmp_path, 'test "$1" = --print-host || exit 9\necho "192.168.8.2"\n')
    assert hostcheck.print_host(binary) == "192.168.8.2"


def test_print_host_runs_with_an_almost_empty_environment(tmp_path: Path) -> None:
    binary = script(tmp_path, 'test -z "$VL_STORE_PATH" && test -z "$HOME" || exit 3\necho h\n')
    assert hostcheck.print_host(binary) == "h"


@pytest.mark.parametrize("body, text", [
    ("echo 'cannot detect' >&2\nexit 1\n", "cannot detect"),
    ("echo a\necho b\n", "not a host"),
    ("echo 'two words'\n", "not a host"),
    ("true\n", "not a host"),
])
def test_print_host_refuses_bad_output(tmp_path: Path, body: str, text: str) -> None:
    with pytest.raises(hostcheck.PrintHostError, match=text):
        hostcheck.print_host(script(tmp_path, body))


def test_an_old_binary_that_starts_a_node_is_killed(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    binary = script(tmp_path, f"echo $$ > {pidfile}\nexec sleep 60\n")
    started = time.monotonic()
    with pytest.raises(hostcheck.PrintHostError, match="did not finish"):
        hostcheck.print_host(binary, timeout=0.5)
    assert time.monotonic() - started < 5
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_missing_binary(tmp_path: Path) -> None:
    with pytest.raises(hostcheck.PrintHostError, match="does not exist"):
        hostcheck.print_host(tmp_path / "nope")


def test_startup_host_prefers_the_override(tmp_path: Path) -> None:
    assert hostcheck.startup_host("  10.0.0.5 ", tmp_path / "nope") == "10.0.0.5"


def test_startup_host_without_a_usable_binary_exits_78_and_asks_for_host(tmp_path: Path) -> None:
    with pytest.raises(AgentExit) as e:
        hostcheck.startup_host(None, tmp_path / "nope")
    assert e.value.code == EXIT_CONFIG and "--host" in e.value.message


def test_drift_ignores_case_and_surrounding_whitespace() -> None:
    assert not hostcheck.drifted("Dev-Box.Local", " dev-box.local\n")
    assert hostcheck.drifted("192.168.8.2", "192.168.68.126")


def test_host_check_same_host_keeps_going(tmp_path: Path) -> None:
    stopper = Stopper()
    host_check_once(script(tmp_path, "echo 192.168.8.2\n"), "192.168.8.2", stopper)
    assert not stopper.is_set()


def test_host_check_changed_host_exits_70_naming_both(tmp_path: Path) -> None:
    stopper = Stopper()
    host_check_once(script(tmp_path, "echo 100.106.199.60\n"), "192.168.8.105", stopper)
    assert stopper.is_set() and stopper.code == EXIT_HOST_CHANGED
    assert "192.168.8.105" in stopper.reason and "100.106.199.60" in stopper.reason


def test_host_check_failure_is_a_warning_not_a_change(tmp_path: Path) -> None:
    stopper = Stopper()
    host_check_once(script(tmp_path, "exit 1\n"), "192.168.8.2", stopper)
    assert not stopper.is_set()


def test_the_first_stop_request_wins() -> None:
    stopper = Stopper()
    stopper.request(EXIT_HOST_CHANGED, "host")
    stopper.request(EXIT_OK, "SIGTERM")
    assert (stopper.code, stopper.reason) == (EXIT_HOST_CHANGED, "host")
