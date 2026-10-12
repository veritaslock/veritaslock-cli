"""Process liveness checks for `vl node` (vl-node-spec.md §4c).

Two checks, deliberately different:

- :func:`is_our_process` — the pid in a ``node`` row: alive, not a zombie, and its
  executable is this node's own ``<node_root>/bin/verilock``. A pid that is alive
  but *not* ours (reused after a reboot) is stale and must never be signalled.
- :func:`is_cp_process` — the pid the control plane reports: alive, not a zombie,
  and named ``verilock``. No executable path is required, since a node started
  outside `vl` (from a build directory, under a debugger) has none under a
  ``<node_root>``.

Both read ``/proc``. Where it is unavailable they degrade to ``kill -0`` alone,
accepting the pid-reuse weakness (vl-node-spec.md §6 item 8). ``proc_root`` is a
parameter so tests can point it at a fake ``/proc`` layout.
"""

from __future__ import annotations

import os
from pathlib import Path

PROC = Path("/proc")
VERILOCK_COMM = "verilock"


def _proc_available(proc_root: Path) -> bool:
    return (proc_root / "self").exists() or (proc_root / "1").exists()


def _stat_fields(pid: int, proc_root: Path) -> list[str] | None:
    """``/proc/<pid>/stat`` from the state field onward, or ``None`` if the pid is gone.

    The command name (field 2) is in parentheses and may itself contain spaces or
    parentheses, so the split starts after the *last* ``)``.
    """
    try:
        raw = (proc_root / str(pid) / "stat").read_text()
    except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
        return None
    except OSError:
        return None
    close = raw.rfind(")")
    if close == -1:
        return None
    return raw[close + 1 :].split()


def _kill0(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def is_alive(pid: int, *, proc_root: Path = PROC) -> bool:
    """The pid exists and is not a zombie (or already dead)."""
    if pid <= 0:
        return False
    if not _proc_available(proc_root):
        return _kill0(pid)
    fields = _stat_fields(pid, proc_root)
    if not fields:
        return False
    return fields[0] not in ("Z", "X", "x")


def _exe(pid: int, proc_root: Path) -> str | None:
    try:
        target = os.readlink(proc_root / str(pid) / "exe")
    except OSError:
        return None  # gone, or not ours to inspect (another user's process)
    # The kernel appends this when the binary was replaced or removed after exec.
    return target.removesuffix(" (deleted)")


def is_our_process(pid: int | None, executable: Path, *, proc_root: Path = PROC) -> bool:
    """``pid`` is alive, not a zombie, and running ``executable``."""
    if pid is None or pid <= 0:
        return False
    if not _proc_available(proc_root):
        return _kill0(pid)
    if not is_alive(pid, proc_root=proc_root):
        return False
    exe = _exe(pid, proc_root)
    if exe is None:
        return False
    return os.path.realpath(exe) == os.path.realpath(executable)


def is_cp_process(pid: int | None, *, proc_root: Path = PROC) -> bool:
    """``pid`` is alive, not a zombie, and named ``verilock``."""
    if pid is None or pid <= 0:
        return False
    if not _proc_available(proc_root):
        return _kill0(pid)
    if not is_alive(pid, proc_root=proc_root):
        return False
    try:
        comm = (proc_root / str(pid) / "comm").read_text().strip()
    except OSError:
        return False
    return comm == VERILOCK_COMM


def process_age_seconds(pid: int | None, *, proc_root: Path = PROC) -> float | None:
    """Seconds since ``pid`` started, or ``None`` when it can't be determined.

    ``starttime`` (field 22 of ``/proc/<pid>/stat``) is in clock ticks since boot;
    ``/proc/uptime`` is seconds since boot.
    """
    if pid is None or pid <= 0 or not _proc_available(proc_root):
        return None
    fields = _stat_fields(pid, proc_root)
    # fields[0] is field 3 (state), so field 22 is fields[19].
    if not fields or len(fields) < 20:
        return None
    try:
        start_ticks = int(fields[19])
        uptime = float((proc_root / "uptime").read_text().split()[0])
        ticks_per_second = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None
    return max(0.0, uptime - start_ticks / ticks_per_second)
