"""Tests for vl.lib.node_state (the §4c displayed-state table) and vl.lib.host."""

from __future__ import annotations

from pathlib import Path

import pytest

from vl.lib import host
from vl.lib.node_state import (
    STARTUP_GRACE_SECONDS,
    CpNode,
    cp_node_from_dto,
    displayed_state,
    parse_cp_pid,
    pids_agree,
)

HERE = "192.168.8.2"
THERE = "10.0.0.9"
OURS = 4312
OTHER = 5555
OLD = 3600.0  # well past the grace period


def _show(
    stored: str,
    stored_pid: int | None,
    cp_state: str | None,
    *,
    cp_pid: int | None = OURS,
    on_this_machine: bool = True,
    cp_pid_alive: bool = False,
    age: float | None = OLD,
) -> str:
    cp = None if cp_state is None else CpNode(cp_state, HERE if on_this_machine else THERE, cp_pid)
    return displayed_state(
        stored, stored_pid, cp,
        on_this_machine=on_this_machine, cp_pid_alive=cp_pid_alive, local_age_seconds=age,
    )


# --------------------------------------------------------------------------- #
# every row of the table
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("case", "kwargs", "expected"),
    [
        # RUNNING / DOWN / pids agree -> NO_HEARTBEAT
        ("running-down-agree", dict(stored="RUNNING", stored_pid=OURS, cp_state="DOWN"), "NO_HEARTBEAT"),
        # RUNNING / DOWN / pids differ -> UNREGISTERED
        ("running-down-other-pid", dict(stored="RUNNING", stored_pid=OURS, cp_state="DOWN", cp_pid=OTHER), "UNREGISTERED"),
        ("running-down-unknown-pid", dict(stored="RUNNING", stored_pid=OURS, cp_state="DOWN", cp_pid=None), "UNREGISTERED"),
        ("running-down-foreign-same-pid", dict(stored="RUNNING", stored_pid=OURS, cp_state="DOWN", on_this_machine=False), "UNREGISTERED"),
        # RUNNING / UP or STANDBY / pids differ -> UNKNOWN
        ("running-up-other-pid", dict(stored="RUNNING", stored_pid=OURS, cp_state="UP", cp_pid=OTHER), "UNKNOWN"),
        ("running-standby-other-pid", dict(stored="RUNNING", stored_pid=OURS, cp_state="STANDBY", cp_pid=OTHER), "UNKNOWN"),
        ("running-up-unknown-pid", dict(stored="RUNNING", stored_pid=OURS, cp_state="UP", cp_pid=None), "UNKNOWN"),
        ("running-up-foreign", dict(stored="RUNNING", stored_pid=OURS, cp_state="UP", on_this_machine=False), "UNKNOWN"),
        # NEW or STOPPED / UP or STANDBY / host not this machine -> REMOTE
        ("stopped-up-foreign", dict(stored="STOPPED", stored_pid=None, cp_state="UP", on_this_machine=False), "REMOTE"),
        ("new-standby-foreign", dict(stored="NEW", stored_pid=None, cp_state="STANDBY", on_this_machine=False), "REMOTE"),
        ("foreign-even-if-pid-alive-here", dict(stored="STOPPED", stored_pid=None, cp_state="UP", on_this_machine=False, cp_pid_alive=True), "REMOTE"),
        # NEW or STOPPED / UP or STANDBY / this machine, CP pid a live verilock -> UNMANAGED
        ("stopped-up-local-alive", dict(stored="STOPPED", stored_pid=None, cp_state="UP", cp_pid_alive=True), "UNMANAGED"),
        ("new-standby-local-alive", dict(stored="NEW", stored_pid=None, cp_state="STANDBY", cp_pid_alive=True), "UNMANAGED"),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_table_rows(case: str, kwargs: dict, expected: str) -> None:
    assert _show(**kwargs) == expected


# --------------------------------------------------------------------------- #
# "anything else": the stored state
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("case", "kwargs", "expected"),
    [
        ("healthy-up", dict(stored="RUNNING", stored_pid=OURS, cp_state="UP"), "RUNNING"),
        ("healthy-standby-catching-up", dict(stored="RUNNING", stored_pid=OURS, cp_state="STANDBY"), "RUNNING"),
        ("cp-has-no-row-new", dict(stored="NEW", stored_pid=None, cp_state=None), "NEW"),
        ("cp-has-no-row-stopped", dict(stored="STOPPED", stored_pid=None, cp_state=None), "STOPPED"),
        ("stopped-vs-up-pid-not-alive-here", dict(stored="STOPPED", stored_pid=None, cp_state="UP", cp_pid_alive=False), "STOPPED"),
        ("new-vs-up-pid-not-alive-here", dict(stored="NEW", stored_pid=None, cp_state="UP", cp_pid_alive=False), "NEW"),
        ("stopped-vs-down", dict(stored="STOPPED", stored_pid=None, cp_state="DOWN", cp_pid_alive=True), "STOPPED"),
        ("new-vs-down", dict(stored="NEW", stored_pid=None, cp_state="DOWN"), "NEW"),
        ("unrecognised-cp-state", dict(stored="RUNNING", stored_pid=OURS, cp_state="DECOMMISSIONED", cp_pid=OTHER), "RUNNING"),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_anything_else_shows_the_stored_state(case: str, kwargs: dict, expected: str) -> None:
    assert _show(**kwargs) == expected


@pytest.mark.parametrize("stored", ["NEW", "RUNNING", "STOPPED"])
def test_cp_unreachable_always_shows_the_stored_state(stored: str) -> None:
    pid = OURS if stored == "RUNNING" else None
    assert _show(stored, pid, None, cp_pid_alive=True, age=0.0) == stored


# --------------------------------------------------------------------------- #
# the grace period suppresses UNREGISTERED only
# --------------------------------------------------------------------------- #


def test_grace_period_suppresses_unregistered() -> None:
    young = STARTUP_GRACE_SECONDS - 0.1
    assert _show("RUNNING", OURS, "DOWN", cp_pid=None, age=young) == "RUNNING"
    assert _show("RUNNING", OURS, "DOWN", cp_pid=OTHER, age=young) == "RUNNING"


def test_grace_period_ends_at_the_constant() -> None:
    assert _show("RUNNING", OURS, "DOWN", cp_pid=None, age=STARTUP_GRACE_SECONDS) == "UNREGISTERED"


def test_unknown_age_does_not_suppress() -> None:
    assert _show("RUNNING", OURS, "DOWN", cp_pid=None, age=None) == "UNREGISTERED"


def test_grace_period_does_not_suppress_other_labels() -> None:
    assert _show("RUNNING", OURS, "DOWN", age=0.5) == "NO_HEARTBEAT"
    assert _show("RUNNING", OURS, "UP", cp_pid=OTHER, age=0.5) == "UNKNOWN"
    assert _show("STOPPED", None, "UP", on_this_machine=False, age=0.5) == "REMOTE"


# --------------------------------------------------------------------------- #
# pids agree
# --------------------------------------------------------------------------- #


def test_pids_agree_needs_this_machine_and_a_known_equal_pid() -> None:
    cp = CpNode("UP", HERE, OURS)
    assert pids_agree(OURS, cp, on_this_machine=True)
    assert not pids_agree(OURS, cp, on_this_machine=False)
    assert not pids_agree(OTHER, cp, on_this_machine=True)
    assert not pids_agree(None, cp, on_this_machine=True)
    assert not pids_agree(OURS, CpNode("UP", HERE, None), on_this_machine=True)


# --------------------------------------------------------------------------- #
# metadataJson's pid
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ('{"pid": "4312"}', 4312),
        ('{"pid": " 4312 "}', 4312),
        ('{"pid": 4312}', 4312),
        ({"pid": "4312"}, 4312),
        ('{"pid": "4312", "version": "1.2"}', 4312),
        (None, None),
        ("", None),
        ("not json", None),
        ("{}", None),
        ('{"pid": null}', None),
        ('{"pid": ""}', None),
        ('{"pid": "abc"}', None),
        ('{"pid": "-5"}', None),
        ('{"pid": "0"}', None),
        ('{"pid": true}', None),
        ('{"pid": 43.12}', None),
        ("[4312]", None),
        ('"4312"', None),
    ],
)
def test_parse_cp_pid_is_lenient(metadata: object, expected: int | None) -> None:
    assert parse_cp_pid(metadata) == expected


def test_cp_node_from_dto() -> None:
    dto = {"state": "UP", "host": HERE, "metadataJson": '{"pid":"4312"}', "orgNode": 1}
    assert cp_node_from_dto(dto) == CpNode("UP", HERE, OURS)
    assert cp_node_from_dto({"state": "DOWN", "host": None, "metadataJson": None}) == CpNode("DOWN", None, None)


def test_an_unknown_cp_pid_never_agrees() -> None:
    cp = cp_node_from_dto({"state": "DOWN", "host": HERE, "metadataJson": "garbage"})
    assert displayed_state(
        "RUNNING", OURS, cp, on_this_machine=True, cp_pid_alive=False, local_age_seconds=OLD
    ) == "UNREGISTERED"


# --------------------------------------------------------------------------- #
# on this machine
# --------------------------------------------------------------------------- #


LOCAL = {"ken-box", "ken-box.example.com", "127.0.0.1", "192.168.8.2", "fe80::3f4a:1484:3e70:38fd", "::1"}


@pytest.mark.parametrize(
    ("cp_host", "expected"),
    [
        ("192.168.8.2", True),
        ("KEN-BOX", True),
        ("ken-box.example.com.", True),
        ("fe80:0:0:0:3f4a:1484:3e70:38fd", True),
        ("fe80::3f4a:1484:3e70:38fd%enp0s31f6", True),
        ("[::1]", True),
        ("10.0.0.9", False),
        ("other-box", False),
        ("", False),
        (None, False),
    ],
)
def test_is_this_machine(cp_host: str | None, expected: bool) -> None:
    assert host.is_this_machine(cp_host, LOCAL) == expected


def test_ipv6_interface_addresses_parse(tmp_path: Path) -> None:
    f = tmp_path / "if_inet6"
    f.write_text(
        "fe800000000000003f4a14843e7038fd 02 40 20 80 enp0s31f6\n"
        "00000000000000000000000000000001 01 80 10 80       lo\n"
        "garbage\n"
    )
    assert host._ipv6_interface_addresses(f) == {"fe80::3f4a:1484:3e70:38fd", "::1"}


def test_local_identities_include_this_hostname_and_loopback() -> None:
    import socket

    local = host.local_identities()
    assert socket.gethostname().lower() in local
    if Path("/proc/net/if_inet6").exists() or Path("/proc/self").exists():
        assert "127.0.0.1" in local
