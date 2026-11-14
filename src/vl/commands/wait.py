"""Waiting for a command's results (vl-agent-cli-spec.md §7.5): one piece of code for
the remote verbs and for `vl command watch`.

**Where the results come from**, decided up front, per org of the command:

- an org for which this host has a **running agent** (its ``agent`` row is ``RUNNING``
  and its pid is that agent): that agent's ``data/results.log``. The agent writes
  every result of its org there, from every host, so the file is a complete source.
  For an issued command the file's position is recorded **before** the request, so a
  result written at once is not missed, and the file is followed across a rotation
  (``resultslog.Follower``). Only rows whose ``id`` is the command's are kept.
- every other org: the control plane, ``GET /v1/commands/{id}`` every ``interval``
  seconds (2 by default), said once.

**When it ends:** every target has answered, or ``waitUntil`` (the 202's, or the
command's) has passed, or an explicit ``timeout`` has run out. Then one final
``GET /v1/commands/{id}`` gives the answer, and **the exit status comes from that
answer, never from the lines printed** (vl.commands.exits).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from vl.agent import config as agent_config
from vl.agent import resultslog
from vl.lib import api, auth, proc, store
from vl.lib.output import console, note, render, warn

EXIT_UNREACHABLE = 4
POLL_SECONDS = 2.0
FILE_POLL_SECONDS = 0.5

# The clock and the pause, as module attributes so the tests can drive them.
_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)  # noqa: E731
_sleep: Callable[[float], None] = time.sleep


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class FileSource:
    org_id: str
    org_name: str
    path: Path
    ino: int | None
    size: int


@dataclass
class Sources:
    """The orgs whose results are read from this host's running agent's file."""

    files: dict[str, FileSource] = field(default_factory=dict)

    @classmethod
    def _collect(cls, environment: store.Environment, orgs: list[store.Organization], *,
                 from_start: bool) -> Sources:
        found: dict[str, FileSource] = {}
        by_name = {o.name: o for o in orgs}
        for agent in store.list_agents(environment.name):
            org = by_name.get(agent.org_name)
            if org is None or agent.state != "RUNNING":
                continue
            label = store.get_identity_by_id(agent.svc_acct_id).label
            if not proc.is_agent_process(agent.pid, label):
                continue
            path = agent_config.results_path(org.name)
            try:
                st = os.stat(path)
                ino, size = st.st_ino, st.st_size
            except FileNotFoundError:
                ino, size = None, 0
            found[org.server_org_id] = FileSource(org.server_org_id, org.name, path, ino,
                                                  0 if from_start else size)
        return cls(found)

    @classmethod
    def before_request(cls, environment: store.Environment, kind: str,
                       organization: store.Organization | None) -> Sources:
        """Recorded before the command is sent: each file's size now."""
        orgs = [organization] if organization is not None else store.list_organizations(environment.name)
        return cls._collect(environment, orgs, from_start=False)

    @classmethod
    def for_issued(cls, environment: store.Environment, org_ids: set[str]) -> Sources:
        """For `vl command watch`: the command was issued before, so read each file whole."""
        orgs = [o for o in store.list_organizations(environment.name) if o.server_org_id in org_ids]
        return cls._collect(environment, orgs, from_start=True)


def get_command(environment: store.Environment, caller: store.Identity, correlation_id: str) -> dict[str, Any]:
    body: dict[str, Any] = auth.authed_call(
        caller, environment.idp_base_url, lambda c: c.get(f"/v1/commands/{correlation_id}"),
        environment.cp_base_url)
    return body


def unreachable(exc: api.ApiError) -> bool:
    return exc.status_code == 0 or exc.status_code >= 500


def stream_line(label: str, outcome: str, when: str, detail: str) -> str:
    """One result as it arrives, in the stream format of `vl agent results --follow`."""
    return f"  {label:<14} {outcome:<15} {when:<24} {detail}"


@dataclass
class Waited:
    """What the wait saw: the lines it printed, and how it ended."""

    printed: dict[tuple[str, int], str] = field(default_factory=dict)
    timed_out: bool = False
    reached_wait_until: bool = False
    interrupted: bool = False


def _org_names(environment: store.Environment) -> tuple[Callable[[str], str], Callable[[str], str]]:
    orgs = store.list_organizations(environment.name)
    names = {o.server_org_id: o.name for o in orgs}
    ids = {o.name: o.server_org_id for o in orgs}
    return (lambda org_id: names.get(org_id, org_id)), (lambda name: ids.get(name, name))


def follow(environment: store.Environment, caller: store.Identity, correlation_id: str,
           targets: list[dict[str, Any]], wait_until: str, *, sources: Sources,
           interval: float = POLL_SECONDS, timeout: float | None = None) -> Waited:
    """Print each target's result as it arrives, until all have answered, ``waitUntil``
    has passed, or ``timeout`` has run out. Ctrl-C ends it with ``interrupted``."""
    name_of, id_of = _org_names(environment)
    expected = {(str(t.get("orgId")), int(t.get("orgNode", 0))) for t in targets}
    org_ids = {o for o, _ in expected}
    file_orgs = {o: s for o, s in sources.files.items() if o in org_ids}
    cp_orgs = sorted(org_ids - set(file_orgs))
    waited = Waited()
    deadline = parse_time(wait_until) or (_now() + timedelta(seconds=60))
    stop_at = deadline if timeout is None else min(deadline, _now() + timedelta(seconds=timeout))

    if cp_orgs:
        note("Waiting on the control plane for: " + ", ".join(name_of(o) for o in cp_orgs)
             + " (no running agent for it on this host)")
    console.print(f"Waiting for {len(expected)} result{'s' if len(expected) != 1 else ''} "
                  f"(Ctrl-C stops waiting; the command keeps running)…")

    followers: list[resultslog.Follower] = []
    pending_lines: list[resultslog.Line] = []
    for src in file_orgs.values():
        f = resultslog.Follower(src.path)
        if src.size == 0:
            # From the start (watch, or a file that did not exist yet): the rotated files
            # too. The current file is read once, by the follower, so nothing falls between.
            current = f.open_at_end()
            pending_lines += list(reversed(resultslog.newest_first(src.path, None, current=current)))
        else:
            pending_lines += f.open_from(src.ino, src.size)
        followers.append(f)

    def take(key: tuple[str, int], outcome: str, when: str, detail: str) -> None:
        if key in expected and key not in waited.printed:
            line = stream_line(f"{name_of(key[0])}/{key[1]}", outcome, when, detail)
            waited.printed[key] = outcome
            console.print(line, markup=False, highlight=False, soft_wrap=True)

    def from_lines(lines: list[resultslog.Line]) -> None:
        for line in lines:
            if line.get("id") != correlation_id:
                continue
            try:
                key = (id_of(line.get("org")), int(line.get("node")))
            except ValueError:
                continue
            if key[0] in file_orgs:
                take(key, line.get("outcome"), line.get("time"), line.get("detail"))

    next_poll = _now()
    warned = False
    try:
        from_lines(pending_lines)
        while True:
            for f in followers:
                from_lines(f.poll())
            if cp_orgs and _now() >= next_poll:
                next_poll = _now() + timedelta(seconds=interval)
                try:
                    answer = get_command(environment, caller, correlation_id)
                except api.ApiError as exc:
                    if not warned:
                        warn(f"The control plane did not answer ({exc}); still waiting.")
                        warned = True
                else:
                    for t in answer.get("targets") or []:
                        key = (str(t.get("orgId")), int(t.get("orgNode", 0)))
                        if key[0] in cp_orgs and t.get("outcome") and t.get("state") != "PENDING":
                            take(key, str(t.get("outcome")), str(t.get("completedAt") or ""),
                                 str(t.get("detail") or ""))
            if expected <= set(waited.printed):
                break
            if _now() >= stop_at:
                waited.timed_out = timeout is not None and stop_at < deadline
                waited.reached_wait_until = not waited.timed_out
                break
            _sleep(FILE_POLL_SECONDS)
    except KeyboardInterrupt:
        waited.interrupted = True
    finally:
        for f in followers:
            f.close()
    return waited


# --------------------------------------------------------------------------- #
# The final answer and what is printed after it
# --------------------------------------------------------------------------- #


def counts_line(answer: dict[str, Any]) -> str:
    c = answer.get("counts") or {}
    total, done, failed = int(c.get("targets", 0)), int(c.get("done", 0)), int(c.get("failed", 0))
    no_answer, skipped, pending = int(c.get("noAnswer", 0)), int(c.get("skipped", 0)), int(c.get("pending", 0))
    if failed:
        nodes = "nodes" if failed > 1 else "node"
        return (f"{done} of {total} succeeded, {failed} failed. Run the same command again to retry the "
                f"{nodes} that failed.")
    parts = []
    if no_answer:
        parts.append(f"{no_answer} did not answer")
    if skipped:
        parts.append(f"{skipped} skipped (no agent)")
    if pending:
        parts.append(f"{pending} not answered yet")
    return f"{done} of {total} succeeded" + ("; " + ", ".join(parts) if parts else "") + "."


def not_succeeded_table(environment: store.Environment, answer: dict[str, Any]) -> None:
    name_of, _ = _org_names(environment)
    rows = [
        {
            "node": f"{name_of(str(t.get('orgId')))}/{t.get('orgNode')}",
            "agent": t.get("agentName") or "-",
            "state": t.get("state"),
            "outcome": t.get("outcome") or "-",
            "exit": t.get("exitCode"),
            "detail": t.get("detail") or "",
            "agent_state": t.get("agentState") or "",
        }
        for t in answer.get("targets") or [] if t.get("state") != "DONE"
    ]
    if rows:
        render(rows, title="Targets that did not succeed")


def wait(environment: store.Environment, caller: store.Identity, correlation_id: str,
         targets: list[dict[str, Any]], wait_until: str, *, sources: Sources,
         interval: float = POLL_SECONDS, timeout: float | None = None) -> int:
    """Follow the results, then the final answer; returns the exit status (§7.6)."""
    from vl.commands import exits

    waited = follow(environment, caller, correlation_id, targets, wait_until, sources=sources,
                    interval=interval, timeout=timeout)
    return exits.finish(environment, caller, correlation_id, waited)
