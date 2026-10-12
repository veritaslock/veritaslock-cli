"""The displayed state of a node (vl-node-spec.md §4c).

The stored ``state`` (``NEW``/``RUNNING``/``STOPPED``) is what `vl` knows about a
process it spawned here; the control plane's ``state`` is what the network knows.
:func:`displayed_state` reconciles the two for display only. Its labels are never
stored, and ``start``/``stop``/``reset`` and the org/network orchestration never
see them.

Everything in this module is pure: the caller gathers the facts (the CP row,
whether its host is this machine, whether its pid is a live ``verilock``, the
local process's age) and passes them in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

STARTUP_GRACE_SECONDS = 10

NO_HEARTBEAT = "NO_HEARTBEAT"
UNREGISTERED = "UNREGISTERED"
REMOTE = "REMOTE"
UNMANAGED = "UNMANAGED"
UNKNOWN = "UNKNOWN"

_CP_LIVE = ("UP", "STANDBY")


@dataclass(frozen=True)
class CpNode:
    """The parts of a control-plane ``NodeDto`` the reconciliation uses."""

    state: str
    host: str | None
    pid: int | None  # None: unknown, which never agrees with anything


def parse_cp_pid(metadata_json: Any) -> int | None:
    """The pid in ``metadataJson``, leniently.

    The CP sends a JSON *string* whose object holds the pid as a string
    (``"{\\"pid\\": \\"4312\\"}"``). An already-decoded object and a numeric pid are
    accepted too. Anything null, missing, non-positive or unparseable is ``None``.
    """
    meta = metadata_json
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            return None
    if not isinstance(meta, dict):
        return None
    raw = meta.get("pid")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        pid = raw
    elif isinstance(raw, str):
        try:
            pid = int(raw.strip())
        except ValueError:
            return None
    else:
        return None
    return pid if pid > 0 else None


def cp_node_from_dto(dto: dict[str, Any]) -> CpNode:
    host = dto.get("host")
    return CpNode(
        state=str(dto.get("state") or ""),
        host=str(host) if host else None,
        pid=parse_cp_pid(dto.get("metadataJson")),
    )


def pids_agree(stored_pid: int | None, cp: CpNode, *, on_this_machine: bool) -> bool:
    """The CP's host is this machine *and* its pid is the stored one.

    Pid numbers from another machine aren't comparable, so a foreign host always
    means "differ"; so does an unknown pid on either side.
    """
    return on_this_machine and cp.pid is not None and stored_pid is not None and cp.pid == stored_pid


def displayed_state(
    stored_state: str,
    stored_pid: int | None,
    cp: CpNode | None,
    *,
    on_this_machine: bool,
    cp_pid_alive: bool,
    local_age_seconds: float | None,
) -> str:
    """The §4c table.

    ``cp`` is ``None`` when the CP has no row for this node or is unreachable.
    ``stored_state``/``stored_pid`` are taken after the repair, so a ``RUNNING`` row
    has a live process of its own. ``cp_pid_alive``: the CP's pid passes the
    "CP's process" check. ``local_age_seconds``: the age of the stored pid's
    process, ``None`` if unknown; a process younger than ``STARTUP_GRACE_SECONDS``
    suppresses ``UNREGISTERED`` only.
    """
    if cp is None:
        return stored_state
    agree = pids_agree(stored_pid, cp, on_this_machine=on_this_machine)

    if stored_state == "RUNNING":
        if cp.state == "DOWN":
            if agree:
                return NO_HEARTBEAT
            starting = local_age_seconds is not None and local_age_seconds < STARTUP_GRACE_SECONDS
            return stored_state if starting else UNREGISTERED
        if cp.state in _CP_LIVE and not agree:
            return UNKNOWN
        return stored_state

    if stored_state in ("NEW", "STOPPED") and cp.state in _CP_LIVE:
        if not on_this_machine:
            return REMOTE
        if cp_pid_alive:
            return UNMANAGED

    return stored_state
