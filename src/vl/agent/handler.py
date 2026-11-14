"""Deciding what to do with a command, and doing it (vl-agent-spec.md §4.2, §4.3, §8 item 8).

The decision order, for each command:

1. **Duplicate**: a ``correlationId`` already handled (kept for an hour) is dropped silently.
2. **Not mine**: only the targets whose ``agentId`` is this agent's are kept; none, and the
   command is ignored silently (no log line, no result). This is the ordinary case.
3. **Inconsistent**: a target that names this agent but another org is refused with a WARN
   and not acted on (the control plane never sends one).
4. **Expired**: ``now > expiresAt`` gives ``EXPIRED`` for each of my targets and nothing else.
   Checked once, here; a command that began in time is carried through. An ``issuedAt`` in
   the future beyond the slack is a WARN about clock skew.
5. **Look up** each target in ``store.db``; one that is not there is ``FAILED``.

Then the targets are acted on in the order listed, only through ``vl.lib.nodeops``. Every
single-node call is wrapped, so an unexpected error is that target's ``FAILED`` with the
message; ``SKIPPED_UP`` is reported as ``FAILED`` with its detail. One result per target,
published as soon as the target is done.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from vl.lib import nodeops, store

DUPLICATE_WINDOW_SECONDS = 3600.0
NOT_FOUND_DETAIL = "node not found on this host"
# The control plane keeps 16,383 characters; the agent keeps its details far shorter.
MAX_DETAIL = 2000


class MalformedCommand(ValueError):
    """A message that decoded but is not a usable ``AgentCommand``."""


@dataclass(frozen=True)
class Target:
    agent_id: str
    org_id: str
    org_node: int


@dataclass(frozen=True)
class Command:
    correlation_id: str
    command_type: str
    command: str
    scope: str
    issued_at: datetime
    expires_at: datetime
    targets: tuple[Target, ...]
    options: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TargetResult:
    org_id: str
    org_node: int
    outcome: str
    detail: str
    exit_code: int | None
    completed_at: datetime


def _utc(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, int) and not isinstance(value, bool):  # timestamp-millis, undecoded
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    raise MalformedCommand(f"{name} is not a timestamp: {value!r}")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise MalformedCommand(f"{name} is missing or not a string: {value!r}")
    return value


def parse_command(value: Any) -> Command:
    """An ``AgentCommand`` as the Avro deserializer returns it (a dict)."""
    if not isinstance(value, dict):
        raise MalformedCommand(f"not a record: {type(value).__name__}")
    raw_targets = value.get("targets")
    if not isinstance(raw_targets, list):
        raise MalformedCommand("targets is not a list")
    targets = []
    for raw in raw_targets:
        if not isinstance(raw, dict) or not isinstance(raw.get("orgNode"), int):
            raise MalformedCommand(f"bad target: {raw!r}")
        targets.append(Target(_text(raw.get("agentId"), "agentId"), _text(raw.get("orgId"), "orgId"),
                              int(raw["orgNode"])))
    command_type = _text(value.get("commandType"), "commandType")
    if command_type not in ("START", "STOP", "RESET"):
        raise MalformedCommand(f"unknown commandType {command_type!r}")
    options = value.get("options") or {}
    if not isinstance(options, dict):
        raise MalformedCommand("options is not a map")
    return Command(
        correlation_id=_text(value.get("correlationId"), "correlationId"),
        command_type=command_type,
        command=_text(value.get("command"), "command"),
        scope=str(value.get("scope") or ""),
        issued_at=_utc(value.get("issuedAt"), "issuedAt"),
        expires_at=_utc(value.get("expiresAt"), "expiresAt"),
        targets=tuple(targets),
        options={str(k): str(v) for k, v in options.items()},
    )


class AgentIdentity:
    """Who this agent is for this run. ``agent_id`` can change if a heartbeat's ``404``
    makes the agent register again, so it is read under a lock."""

    def __init__(self, *, agent_id: str, name: str, host: str, org_id: str, org_name: str) -> None:
        self._lock = threading.Lock()
        self._agent_id = agent_id
        self.name = name
        self.host = host
        self.org_id = org_id
        self.org_name = org_name

    @property
    def agent_id(self) -> str:
        with self._lock:
            return self._agent_id

    @agent_id.setter
    def agent_id(self, value: str) -> None:
        with self._lock:
            self._agent_id = value


@dataclass
class NodeOps:
    """The calls the handler makes, all from ``vl.lib``; replaceable in tests."""

    get_node: Callable[[str, str, int], store.Node] = store.get_node
    start_node: Callable[..., nodeops.NodeResult] = nodeops.start_node
    stop_nodes_to_down: Callable[..., list[tuple[store.Node, nodeops.NodeResult]]] = nodeops.stop_nodes_to_down
    reset_node: Callable[..., nodeops.NodeResult] = nodeops.reset_node
    is_our_process: Callable[[store.Node], bool] = nodeops.is_our_process


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _message(exc: BaseException) -> str:
    text = str(exc).strip()
    return text if text else type(exc).__name__


def _short(detail: str) -> str:
    return detail if len(detail) <= MAX_DETAIL else detail[: MAX_DETAIL - 1] + "…"


Publish = Callable[[Command, TargetResult], None]


class CommandHandler:
    """Handles one command at a time, on the consumer's thread."""

    def __init__(
        self,
        identity: AgentIdentity,
        environment: store.Environment,
        publish: Publish,
        log: logging.Logger,
        *,
        skew_warn_seconds: float = 5.0,
        ops: NodeOps | None = None,
        clock: Callable[[], datetime] = _now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._identity = identity
        self._env = environment
        self._publish = publish
        self._log = log
        self._skew = timedelta(seconds=skew_warn_seconds)
        self._ops = ops or NodeOps()
        self._clock = clock
        self._monotonic = monotonic
        self._seen: dict[str, float] = {}
        self._waitpid = os.waitpid
        # Nodes this agent spawned: its children until they exit, reaped by reap_children().
        self._children: set[int] = set()

    # -- the decision order --------------------------------------------------

    def _duplicate(self, correlation_id: str) -> bool:
        now = self._monotonic()
        for cid in [c for c, t in self._seen.items() if now - t > DUPLICATE_WINDOW_SECONDS]:
            del self._seen[cid]
        if correlation_id in self._seen:
            return True
        self._seen[correlation_id] = now
        return False

    def handle(self, cmd: Command) -> None:
        if self._duplicate(cmd.correlation_id):
            return
        my_id = self._identity.agent_id
        mine = [t for t in cmd.targets if t.agent_id == my_id]
        if not mine:
            return
        consistent = []
        for t in mine:
            if t.org_id != self._identity.org_id:
                self._log.warning(
                    "command %s: target %s/%d names this agent but another org (this agent serves %s); "
                    "refused, not acted on",
                    cmd.correlation_id, t.org_id, t.org_node, self._identity.org_id,
                )
            else:
                consistent.append(t)
        if not consistent:
            return

        now = self._clock()
        if cmd.issued_at - now > self._skew:
            self._log.warning(
                "command %s was issued %.1f s in the future by the control plane's clock: "
                "check that this host's clock is synchronized (NTP)",
                cmd.correlation_id, (cmd.issued_at - now).total_seconds(),
            )
        if now > cmd.expires_at:
            self._log.warning(
                "command %s (%s) expired at %s, received at %s: EXPIRED for %d target(s)",
                cmd.correlation_id, cmd.command, cmd.expires_at.isoformat(), now.isoformat(), len(consistent),
            )
            for t in consistent:
                self._emit_result(cmd, t, "EXPIRED", f"received after the command expired at "
                                  f"{cmd.expires_at.isoformat()}", None)
            return

        self._log.info("command %s: %s (%s) for %d node(s): %s", cmd.correlation_id, cmd.command_type,
                       cmd.command, len(consistent), ", ".join(str(t.org_node) for t in consistent))
        if cmd.command_type == "START":
            for t in consistent:
                self._start(cmd, t)
        elif cmd.command_type == "RESET":
            for t in consistent:
                self._reset(cmd, t)
        else:
            self._stop(cmd, consistent)

    # -- results ---------------------------------------------------------------

    def _emit_result(self, cmd: Command, t: Target, outcome: str, detail: str, exit_code: int | None) -> None:
        result = TargetResult(t.org_id, t.org_node, outcome, _short(detail), exit_code, self._clock())
        self._log.info("command %s: %s/node%d %s: %s", cmd.correlation_id, self._identity.org_name,
                       t.org_node, outcome, result.detail)
        try:
            self._publish(cmd, result)
        except Exception as exc:  # the publisher logs its own failures; never stop the queue
            self._log.error("command %s: could not publish the result for node %d: %s",
                            cmd.correlation_id, t.org_node, _message(exc))

    def reap_children(self) -> None:
        """Collect the exit status of nodes this agent started that have since exited.

        A node is spawned in its own session, but its parent is still this process, so a
        node that exits stays a zombie until it is waited for. Only those pids are waited
        for, never ``-1``: that would also take the status of ``verilock --print-host``
        from the host check's own wait.
        """
        for pid in list(self._children):
            try:
                done, _status = self._waitpid(pid, os.WNOHANG)
            except ChildProcessError:  # already collected (subprocess does it too)
                done = pid
            if done == pid:
                self._children.discard(pid)

    def _report(self, cmd: Command, t: Target, result: nodeops.NodeResult) -> None:
        outcome = result.outcome
        if outcome is nodeops.Outcome.STARTED and result.pid is not None:
            self._children.add(result.pid)
        if outcome is nodeops.Outcome.SKIPPED_UP:
            self._emit_result(cmd, t, "FAILED", result.detail, result.exit_code)
        else:
            self._emit_result(cmd, t, outcome.value, result.detail, result.exit_code)

    # -- acting ---------------------------------------------------------------

    def _emit_progress(self, text: str) -> None:
        self._log.info("%s", text)

    def _lookup(self, cmd: Command, t: Target) -> store.Node | None:
        try:
            return self._ops.get_node(self._env.name, self._identity.org_name, t.org_node)
        except store.NodeNotFoundError:
            self._emit_result(cmd, t, "FAILED", NOT_FOUND_DETAIL, None)
        except Exception as exc:
            self._emit_result(cmd, t, "FAILED", _message(exc), None)
        return None

    def _guarded(self, call: Callable[[], nodeops.NodeResult]) -> nodeops.NodeResult:
        try:
            return call()
        except Exception as exc:
            return nodeops.NodeResult(nodeops.Outcome.FAILED, _message(exc))

    def _start(self, cmd: Command, t: Target) -> None:
        if self._lookup(cmd, t) is None:
            return
        result = self._guarded(lambda: self._ops.start_node(
            self._env, self._identity.org_name, t.org_node, self._emit_progress))
        self._report(cmd, t, result)

    def _reset(self, cmd: Command, t: Target) -> None:
        node = self._lookup(cmd, t)
        if node is None:
            return
        try:
            alive = node.node_state == "RUNNING" and node.pid is not None and self._ops.is_our_process(node)
        except Exception as exc:
            self._emit_result(cmd, t, "FAILED", _message(exc), None)
            return
        if alive:
            self._emit_result(cmd, t, "FAILED", f"node process is running (pid {node.pid})", None)
            return
        include = cmd.options.get("includeSnapshots", "").lower() == "true"
        result = self._guarded(lambda: self._ops.reset_node(
            self._env, self._identity.org_name, t.org_node, self._emit_progress, include_snapshots=include))
        self._report(cmd, t, result)

    def _stop(self, cmd: Command, targets: list[Target]) -> None:
        found: list[tuple[Target, store.Node]] = []
        for t in targets:
            node = self._lookup(cmd, t)
            if node is not None:
                found.append((t, node))
        if not found:
            return
        by_node = {node.org_node: t for t, node in found}
        reported: set[int] = set()

        def on_result(node: store.Node, result: nodeops.NodeResult) -> None:
            reported.add(node.org_node)
            self._report(cmd, by_node[node.org_node], result)

        # stop_nodes_to_down stops in the reverse of the list it is given; the control
        # plane lists a STOP's targets descending, so hand it them ascending.
        ascending = [node for _, node in reversed(found)]
        try:
            self._ops.stop_nodes_to_down(self._env, ascending, self._emit_progress, on_result=on_result)
        except Exception as exc:  # it never raises for a node; this is a last resort
            for t, node in found:
                if node.org_node not in reported:
                    self._emit_result(cmd, t, "FAILED", _message(exc), None)
