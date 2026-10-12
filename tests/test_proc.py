"""Tests for vl.lib.proc — §4c liveness checks against fake /proc layouts."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vl.lib import proc

TICKS = os.sysconf("SC_CLK_TCK")


def _fake_proc(tmp_path: Path, *, uptime: float = 1000.0) -> Path:
    root = tmp_path / "proc"
    (root / "self").mkdir(parents=True)
    (root / "uptime").write_text(f"{uptime} 4000.00\n")
    return root


def _add_pid(
    root: Path,
    pid: int,
    *,
    state: str = "S",
    comm: str = "verilock",
    exe: Path | str | None = None,
    start_seconds: float = 900.0,
) -> None:
    d = root / str(pid)
    d.mkdir()
    # Fields 3..22 after "(comm)": state, 18 fillers, then starttime (field 22).
    after = [state] + ["0"] * 18 + [str(int(start_seconds * TICKS))] + ["0"] * 10
    (d / "stat").write_text(f"{pid} ({comm}) {' '.join(after)}\n")
    (d / "comm").write_text(comm + "\n")
    if exe is not None:
        os.symlink(str(exe), d / "exe")


@pytest.fixture
def binary(tmp_path: Path) -> Path:
    b = tmp_path / "orgs" / "globo" / "nodes" / "node1" / "bin" / "verilock"
    b.parent.mkdir(parents=True)
    b.write_text("")
    return b


# --------------------------------------------------------------------------- #
# "our process"
# --------------------------------------------------------------------------- #


def test_our_process_when_exe_is_this_nodes_binary(tmp_path: Path, binary: Path) -> None:
    root = _fake_proc(tmp_path)
    _add_pid(root, 4312, exe=binary)
    assert proc.is_our_process(4312, binary, proc_root=root)


def test_our_process_tolerates_a_replaced_binary(tmp_path: Path, binary: Path) -> None:
    root = _fake_proc(tmp_path)
    _add_pid(root, 4312, exe=f"{binary} (deleted)")
    assert proc.is_our_process(4312, binary, proc_root=root)


def test_reused_pid_running_something_else_is_not_ours(tmp_path: Path, binary: Path) -> None:
    root = _fake_proc(tmp_path)
    _add_pid(root, 4312, comm="bash", exe="/usr/bin/bash")
    assert not proc.is_our_process(4312, binary, proc_root=root)


def test_another_nodes_verilock_is_not_ours(tmp_path: Path, binary: Path) -> None:
    root = _fake_proc(tmp_path)
    other = tmp_path / "orgs" / "globo" / "nodes" / "node2" / "bin" / "verilock"
    other.parent.mkdir(parents=True)
    other.write_text("")
    _add_pid(root, 4312, exe=other)
    assert not proc.is_our_process(4312, binary, proc_root=root)


def test_zombie_is_not_ours(tmp_path: Path, binary: Path) -> None:
    root = _fake_proc(tmp_path)
    _add_pid(root, 4312, state="Z", exe=binary)
    assert not proc.is_our_process(4312, binary, proc_root=root)


def test_missing_pid_is_not_ours(tmp_path: Path, binary: Path) -> None:
    root = _fake_proc(tmp_path)
    assert not proc.is_our_process(4312, binary, proc_root=root)


def test_unreadable_exe_is_not_ours(tmp_path: Path, binary: Path) -> None:
    """Another user's process: /proc/<pid>/exe can't be read, so it can't be confirmed."""
    root = _fake_proc(tmp_path)
    _add_pid(root, 4312, exe=None)
    assert not proc.is_our_process(4312, binary, proc_root=root)


@pytest.mark.parametrize("pid", [None, 0, -1])
def test_no_pid_is_not_ours(tmp_path: Path, binary: Path, pid: int | None) -> None:
    assert not proc.is_our_process(pid, binary, proc_root=_fake_proc(tmp_path))


# --------------------------------------------------------------------------- #
# "the CP's process"
# --------------------------------------------------------------------------- #


def test_cp_process_needs_only_the_name(tmp_path: Path) -> None:
    root = _fake_proc(tmp_path)
    _add_pid(root, 777, exe="/home/dev/build/verilock")  # not under any node_root
    assert proc.is_cp_process(777, proc_root=root)


def test_cp_process_rejects_other_names_zombies_and_missing(tmp_path: Path) -> None:
    root = _fake_proc(tmp_path)
    _add_pid(root, 777, comm="python3")
    _add_pid(root, 778, state="Z")
    assert not proc.is_cp_process(777, proc_root=root)
    assert not proc.is_cp_process(778, proc_root=root)
    assert not proc.is_cp_process(779, proc_root=root)
    assert not proc.is_cp_process(None, proc_root=root)


def test_comm_with_spaces_and_parens_does_not_confuse_stat_parsing(tmp_path: Path) -> None:
    root = _fake_proc(tmp_path)
    _add_pid(root, 777, comm="a (b) c", state="Z")
    assert not proc.is_alive(777, proc_root=root)


# --------------------------------------------------------------------------- #
# process age
# --------------------------------------------------------------------------- #


def test_process_age_from_starttime_and_uptime(tmp_path: Path) -> None:
    root = _fake_proc(tmp_path, uptime=1000.0)
    _add_pid(root, 4312, start_seconds=995.0)
    age = proc.process_age_seconds(4312, proc_root=root)
    assert age is not None and abs(age - 5.0) < 0.1


def test_process_age_unknown_for_missing_pid(tmp_path: Path) -> None:
    assert proc.process_age_seconds(4312, proc_root=_fake_proc(tmp_path)) is None


# --------------------------------------------------------------------------- #
# no /proc: kill -0 fallback
# --------------------------------------------------------------------------- #


def test_without_proc_both_checks_fall_back_to_kill0(
    tmp_path: Path, binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_proc = tmp_path / "missing"
    alive = {4312}

    def fake_kill(pid: int, sig: int) -> None:
        assert sig == 0
        if pid not in alive:
            raise ProcessLookupError

    monkeypatch.setattr(proc.os, "kill", fake_kill)
    assert proc.is_our_process(4312, binary, proc_root=no_proc)
    assert proc.is_cp_process(4312, proc_root=no_proc)
    assert not proc.is_our_process(9999, binary, proc_root=no_proc)
    assert proc.process_age_seconds(4312, proc_root=no_proc) is None


def test_kill0_permission_error_counts_as_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_kill(pid: int, sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(proc.os, "kill", fake_kill)
    assert proc.is_alive(1, proc_root=tmp_path / "missing")


def test_real_proc_sees_this_process() -> None:
    """Sanity check against the real /proc on Linux."""
    if not Path("/proc/self").exists():
        pytest.skip("no /proc")
    me = os.getpid()
    assert proc.is_alive(me)
    assert proc.is_our_process(me, Path(os.readlink("/proc/self/exe")))
    age = proc.process_age_seconds(me)
    assert age is not None and age >= 0
