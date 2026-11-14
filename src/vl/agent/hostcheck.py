"""The agent's ``host`` (vl-agent-spec.md §3, §8 items 2 and 7).

The host comes from ``verilock --print-host``, the node's own detection, so the
agent and its nodes agree by construction (the control plane matches them by
org and host). ``--host`` overrides it, and then there is nothing to re-check.

Every 60 seconds the agent runs ``--print-host`` again; a value that differs
(trimmed and lower-cased, as the control plane compares) from the host it
registered with makes it exit 70, so that it is restarted and re-registers. It
never changes its host silently.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from pathlib import Path

from vl.agent import EXIT_CONFIG, AgentExit

PRINT_HOST_TIMEOUT_SECONDS = 5.0
# An IPv4 address or a host name, as select_host() returns: one token, no spaces.
_HOST_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")


class PrintHostError(Exception):
    """``verilock --print-host`` gave no usable host."""


def normalize(host: str) -> str:
    """As the control plane stores and compares hosts: trimmed, lower case."""
    return host.strip().lower()


def print_host(binary: Path, *, timeout: float = PRINT_HOST_TIMEOUT_SECONDS) -> str:
    """Run ``<binary> --print-host`` and return the host it prints.

    The binary gets an almost empty environment and its own session, and is
    killed (with its group) after ``timeout`` seconds: a binary that predates
    the flag ignores it and starts a node instead, and must not be left running.
    """
    if not binary.is_file():
        raise PrintHostError(f"{binary} does not exist")
    try:
        child = subprocess.Popen(
            [str(binary), "--print-host"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            start_new_session=True,
        )
    except OSError as exc:
        raise PrintHostError(f"cannot run {binary}: {exc}") from exc
    try:
        out, err = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        child.communicate()
        raise PrintHostError(
            f"{binary} --print-host did not finish within {timeout:g} s "
            f"(a verilock older than the flag starts a node instead)"
        ) from None
    text = out.decode("utf-8", errors="replace").strip()
    if child.returncode != 0:
        reason = err.decode("utf-8", errors="replace").strip() or f"exit status {child.returncode}"
        raise PrintHostError(f"{binary} --print-host failed: {reason}")
    lines = text.splitlines()
    if len(lines) != 1 or not _HOST_PATTERN.match(lines[0].strip()):
        raise PrintHostError(f"{binary} --print-host printed {text!r}, not a host")
    return lines[0].strip()


def startup_host(override: str | None, binary: Path) -> str:
    """The host to register with: ``--host``, else ``--print-host``. A binary that
    cannot tell is a configuration error (exit 78) that asks for ``--host``."""
    if override is not None:
        return override.strip()
    try:
        return print_host(binary)
    except PrintHostError as exc:
        raise AgentExit(
            EXIT_CONFIG,
            f"cannot work out this host: {exc}. Install a verilock with --print-host, "
            f"pass --verilock <path>, or pass --host <address>.",
        ) from exc


def drifted(registered: str, current: str) -> bool:
    """Whether ``current`` names a different host than ``registered``, as the control plane compares."""
    return normalize(registered) != normalize(current)
