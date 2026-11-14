"""Command handling (vl-agent-spec.md §4.2, §4.3, §8 item 8), with fakes for the node
operations, the publisher and the clock."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from vl.agent.handler import (
    NOT_FOUND_DETAIL,
    AgentIdentity,
    Command,
    CommandHandler,
    MalformedCommand,
    NodeOps,
    Target,
    TargetResult,
    parse_command,
)
from vl.lib import nodelock, nodeops, store
from vl.lib.nodeops import NodeResult, Outcome

ME = "agent-me"
ORG = "org-1"
T0 = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
ENV = store.Environment("local", "http://idp", "http://cp", "http://di", "http://tok", "k:9092",
                        "http://sr", True, "x")


def node(n: int, state: str = "STOPPED", pid: int | None = None, org: str = "globo") -> store.Node:
    return store.Node(id=n, environment_name="local", org_name=org, org_node=n, host=None,
                      svc_acct_id=1, port=7000 + n, pid=pid, node_state=state, created_at="x")


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class Fakes:
    """Records every call, in order."""

    def __init__(self, nodes: dict[int, store.Node]) -> None:
        self.nodes = nodes
        self.calls: list[tuple[Any, ...]] = []
        self.start_results: dict[int, NodeResult | Exception] = {}
        self.reset_results: dict[int, NodeResult | Exception] = {}
        self.stop_results: dict[int, NodeResult] = {}
        self.alive: set[int] = set()
        self.stop_raises: Exception | None = None
        self.clock: Clock | None = None
        self.advance_on_start = timedelta(0)

    def get_node(self, env: str, org: str, n: int) -> store.Node:
        self.calls.append(("get", org, n))
        if n not in self.nodes:
            raise store.NodeNotFoundError(f"No node {org}/node{n}")
        return self.nodes[n]

    def start_node(self, env: store.Environment, org: str, n: int, emit: Any, **kw: Any) -> NodeResult:
        self.calls.append(("start", org, n))
        if self.clock is not None:
            self.clock.now += self.advance_on_start
        r = self.start_results.get(n, NodeResult(Outcome.STARTED, f"pid {100 + n}", pid=100 + n))
        if isinstance(r, Exception):
            raise r
        return r

    def reset_node(self, env: store.Environment, org: str, n: int, emit: Any, *,
                   include_snapshots: bool = False, **kw: Any) -> NodeResult:
        self.calls.append(("reset", org, n, include_snapshots))
        r = self.reset_results.get(n, NodeResult(Outcome.RESET, "deleted its data directory"))
        if isinstance(r, Exception):
            raise r
        return r

    def stop_nodes_to_down(self, env: store.Environment, nodes: list[store.Node], emit: Any, *,
                           on_result: Any = None, **kw: Any) -> list[tuple[store.Node, NodeResult]]:
        self.calls.append(("stop_nodes_to_down", [x.org_node for x in nodes]))
        out = []
        for x in reversed(nodes):  # as the real one does
            if self.stop_raises is not None:
                raise self.stop_raises
            r = self.stop_results.get(x.org_node, NodeResult(Outcome.STOPPED, "pid 1", pid=1))
            out.append((x, r))
            if on_result is not None:
                on_result(x, r)
        return out

    def is_our_process(self, n: store.Node) -> bool:
        return n.org_node in self.alive

    def ops(self) -> NodeOps:
        return NodeOps(get_node=self.get_node, start_node=self.start_node,
                       stop_nodes_to_down=self.stop_nodes_to_down, reset_node=self.reset_node,
                       is_our_process=self.is_our_process)


class Published(list[tuple[Command, TargetResult]]):
    def __call__(self, cmd: Command, result: TargetResult) -> None:
        self.append((cmd, result))

    def outcomes(self) -> list[tuple[int, str]]:
        return [(r.org_node, r.outcome) for _, r in self]


def cmd(kind: str, targets: list[tuple[str, str, int]], *, cid: str = "c-1", issued: datetime = T0,
        ttl: float = 30, options: dict[str, str] | None = None) -> Command:
    return Command(correlation_id=cid, command_type=kind, command=f"vl org {kind.lower()}", scope="ORG",
                   issued_at=issued, expires_at=issued + timedelta(seconds=ttl),
                   targets=tuple(Target(a, o, n) for a, o, n in targets), options=options or {})


@pytest.fixture
def setup() -> tuple[CommandHandler, Fakes, Published, Clock]:
    fakes = Fakes({n: node(n) for n in (1, 2, 3, 4)})
    published = Published()
    clock = Clock()
    fakes.clock = clock
    me = AgentIdentity(agent_id=ME, name="dev-box", host="192.168.8.2", org_id=ORG, org_name="globo")
    handler = CommandHandler(me, ENV, published, logging.getLogger("test.agent"), ops=fakes.ops(),
                             clock=clock)
    return handler, fakes, published, clock


# --- 1. duplicate ----------------------------------------------------------------


def test_a_duplicate_is_dropped(setup: Any) -> None:
    handler, fakes, published, _ = setup
    c = cmd("START", [(ME, ORG, 1)])
    handler.handle(c)
    handler.handle(c)
    assert published.outcomes() == [(1, "STARTED")]
    assert [x for x in fakes.calls if x[0] == "start"] == [("start", "globo", 1)]


def test_a_duplicate_is_forgotten_after_an_hour(setup: Any) -> None:
    _, fakes, published, clock = setup
    t = [0.0]
    me = AgentIdentity(agent_id=ME, name="n", host="h", org_id=ORG, org_name="globo")
    handler = CommandHandler(me, ENV, published, logging.getLogger("t"), ops=fakes.ops(), clock=clock,
                             monotonic=lambda: t[0])
    c = cmd("START", [(ME, ORG, 1)], ttl=10_000)
    handler.handle(c)
    t[0] = 3601.0
    handler.handle(c)
    assert published.outcomes() == [(1, "STARTED"), (1, "STARTED")]


# --- 2. not mine ------------------------------------------------------------------


def test_not_mine_is_ignored_silently(setup: Any, caplog: pytest.LogCaptureFixture) -> None:
    handler, fakes, published, _ = setup
    with caplog.at_level(logging.DEBUG, logger="test.agent"):
        handler.handle(cmd("STOP", [("agent-other", ORG, 1), ("agent-other", "org-9", 2)], ttl=-5))
    assert published == []
    assert fakes.calls == []
    assert caplog.records == []


def test_only_my_targets_are_acted_on(setup: Any) -> None:
    handler, fakes, published, _ = setup
    handler.handle(cmd("START", [("agent-other", ORG, 1), (ME, ORG, 2), ("agent-other", ORG, 3)]))
    assert published.outcomes() == [(2, "STARTED")]


# --- 3. inconsistent ----------------------------------------------------------------


def test_a_target_of_another_org_is_refused_with_a_warning(setup: Any, caplog: pytest.LogCaptureFixture) -> None:
    handler, fakes, published, _ = setup
    with caplog.at_level(logging.WARNING, logger="test.agent"):
        handler.handle(cmd("START", [(ME, "org-9", 1), (ME, ORG, 2)]))
    assert published.outcomes() == [(2, "STARTED")]
    assert ("get", "globo", 1) not in fakes.calls  # never looked up, not even under my org
    assert any("another org" in r.message and r.levelno == logging.WARNING for r in caplog.records)


# --- 4. expired --------------------------------------------------------------------


def test_expired_gives_expired_for_each_of_my_targets_and_does_nothing(setup: Any) -> None:
    handler, fakes, published, clock = setup
    clock.now = T0 + timedelta(seconds=31)
    handler.handle(cmd("STOP", [(ME, ORG, 2), ("agent-other", ORG, 3), (ME, ORG, 1)]))
    assert published.outcomes() == [(2, "EXPIRED"), (1, "EXPIRED")]
    assert fakes.calls == []


def test_exactly_at_expiry_is_not_expired(setup: Any) -> None:
    handler, fakes, published, clock = setup
    clock.now = T0 + timedelta(seconds=30)
    handler.handle(cmd("START", [(ME, ORG, 1)]))
    assert published.outcomes() == [(1, "STARTED")]


def test_expiry_is_checked_only_at_the_start(setup: Any) -> None:
    handler, fakes, published, clock = setup
    fakes.advance_on_start = timedelta(seconds=20)  # each start takes 20 s of the 30
    handler.handle(cmd("START", [(ME, ORG, 1), (ME, ORG, 2), (ME, ORG, 3)]))
    assert published.outcomes() == [(1, "STARTED"), (2, "STARTED"), (3, "STARTED")]


def test_clock_skew_is_a_warning_and_the_command_still_runs(setup: Any, caplog: pytest.LogCaptureFixture) -> None:
    handler, fakes, published, _ = setup
    with caplog.at_level(logging.WARNING, logger="test.agent"):
        handler.handle(cmd("START", [(ME, ORG, 1)], issued=T0 + timedelta(seconds=6)))
    assert published.outcomes() == [(1, "STARTED")]
    assert any("NTP" in r.message for r in caplog.records)


def test_no_skew_warning_within_the_slack(setup: Any, caplog: pytest.LogCaptureFixture) -> None:
    handler, fakes, published, _ = setup
    with caplog.at_level(logging.WARNING, logger="test.agent"):
        handler.handle(cmd("START", [(ME, ORG, 1)], issued=T0 + timedelta(seconds=4)))
    assert not any("NTP" in r.message for r in caplog.records)


# --- 5. look up, and acting in order --------------------------------------------------


def test_an_unknown_node_is_failed_and_the_rest_go_on(setup: Any) -> None:
    handler, fakes, published, _ = setup
    handler.handle(cmd("START", [(ME, ORG, 1), (ME, ORG, 9), (ME, ORG, 2)]))
    assert published.outcomes() == [(1, "STARTED"), (9, "FAILED"), (2, "STARTED")]
    assert published[1][1].detail == NOT_FOUND_DETAIL


def test_start_targets_in_the_order_listed(setup: Any) -> None:
    handler, fakes, published, _ = setup
    handler.handle(cmd("START", [(ME, ORG, 3), (ME, ORG, 1), (ME, ORG, 2)]))
    assert [x for x in fakes.calls if x[0] == "start"] == [("start", "globo", 3), ("start", "globo", 1),
                                                          ("start", "globo", 2)]


def test_outcomes_are_reported_as_they_are(setup: Any) -> None:
    handler, fakes, published, _ = setup
    fakes.start_results[2] = NodeResult(Outcome.ALREADY_RUNNING, "already running (pid 7)", pid=7)
    fakes.start_results[3] = NodeResult(
        Outcome.FAILED, "exited right after starting (exited with code 69). The control plane still has "
                        "this node as UP.", exit_code=69)
    handler.handle(cmd("START", [(ME, ORG, 1), (ME, ORG, 2), (ME, ORG, 3)]))
    assert published.outcomes() == [(1, "STARTED"), (2, "ALREADY_RUNNING"), (3, "FAILED")]
    failed = published[2][1]
    assert failed.exit_code == 69 and "still has this node as UP" in failed.detail
    assert published[0][1].detail == "pid 101" and published[0][1].exit_code is None


def test_an_unexpected_error_is_that_targets_failed_with_the_message(setup: Any) -> None:
    handler, fakes, published, _ = setup
    fakes.start_results[1] = store.StoreError("database is locked")
    fakes.start_results[2] = RuntimeError()
    handler.handle(cmd("START", [(ME, ORG, 1), (ME, ORG, 2), (ME, ORG, 3)]))
    assert [(r.org_node, r.outcome, r.detail) for _, r in published] == [
        (1, "FAILED", "database is locked"), (2, "FAILED", "RuntimeError"), (3, "STARTED", "pid 103")]


def test_a_busy_node_is_failed(setup: Any) -> None:
    handler, fakes, published, _ = setup
    fakes.start_results[1] = NodeResult(Outcome.FAILED, nodelock.BUSY_DETAIL)
    handler.handle(cmd("START", [(ME, ORG, 1)]))
    assert [(r.outcome, r.detail) for _, r in published] == [("FAILED", "another operation is in progress")]


def test_a_publish_that_raises_does_not_stop_the_command(setup: Any) -> None:
    _, fakes, _, clock = setup
    seen: list[int] = []

    def flaky(c: Command, r: TargetResult) -> None:
        seen.append(r.org_node)
        raise RuntimeError("broker gone")

    me = AgentIdentity(agent_id=ME, name="n", host="h", org_id=ORG, org_name="globo")
    handler = CommandHandler(me, ENV, flaky, logging.getLogger("t"), ops=fakes.ops(), clock=clock)
    handler.handle(cmd("START", [(ME, ORG, 1), (ME, ORG, 2)]))
    assert seen == [1, 2]


# --- RESET -----------------------------------------------------------------------------


def test_reset_skipped_up_is_reported_as_failed_with_its_detail(setup: Any) -> None:
    handler, fakes, published, _ = setup
    fakes.reset_results[1] = NodeResult(Outcome.SKIPPED_UP, nodeops.STILL_UP_DETAIL)
    handler.handle(cmd("RESET", [(ME, ORG, 1), (ME, ORG, 2)]))
    assert [(r.org_node, r.outcome, r.detail) for _, r in published] == [
        (1, "FAILED", "still reported UP by the control plane"), (2, "RESET", "deleted its data directory")]


def test_reset_is_refused_while_the_process_is_alive_and_never_stops_it(setup: Any) -> None:
    handler, fakes, published, _ = setup
    fakes.nodes[1] = node(1, "RUNNING", pid=555)
    fakes.alive.add(1)
    handler.handle(cmd("RESET", [(ME, ORG, 1)]))
    assert [(r.outcome, r.detail) for _, r in published] == [("FAILED", "node process is running (pid 555)")]
    assert not any(x[0] in ("reset", "stop_nodes_to_down") for x in fakes.calls)


def test_reset_of_a_stale_running_row_goes_ahead(setup: Any) -> None:
    handler, fakes, published, _ = setup
    fakes.nodes[1] = node(1, "RUNNING", pid=555)  # the pid is not its process any more
    handler.handle(cmd("RESET", [(ME, ORG, 1)]))
    assert published.outcomes() == [(1, "RESET")]


@pytest.mark.parametrize("options, expected", [({}, False), ({"includeSnapshots": "true"}, True),
                                               ({"includeSnapshots": "false"}, False)])
def test_reset_include_snapshots_option(setup: Any, options: dict[str, str], expected: bool) -> None:
    handler, fakes, published, _ = setup
    handler.handle(cmd("RESET", [(ME, ORG, 1)], options=options))
    assert ("reset", "globo", 1, expected) in fakes.calls


# --- STOP ------------------------------------------------------------------------------


def test_stop_keeps_the_listed_descending_order(setup: Any) -> None:
    handler, fakes, published, _ = setup
    handler.handle(cmd("STOP", [(ME, ORG, 4), (ME, ORG, 3), (ME, ORG, 1)]))
    # stop_nodes_to_down reverses its list, so it is handed the nodes ascending ...
    assert ("stop_nodes_to_down", [1, 3, 4]) in fakes.calls
    # ... and stops, and so reports, in the control plane's descending order.
    assert published.outcomes() == [(4, "STOPPED"), (3, "STOPPED"), (1, "STOPPED")]


def test_stop_results_are_published_one_by_one(setup: Any) -> None:
    handler, fakes, published, _ = setup
    fakes.stop_results[3] = NodeResult(Outcome.FAILED, nodeops.STILL_UP_DETAIL)
    fakes.stop_results[2] = NodeResult(Outcome.ALREADY_STOPPED, "not running")
    handler.handle(cmd("STOP", [(ME, ORG, 3), (ME, ORG, 2), (ME, ORG, 9)]))
    assert published.outcomes() == [(9, "FAILED"), (3, "FAILED"), (2, "ALREADY_STOPPED")]


def test_stop_that_raises_fails_every_target_not_yet_reported(setup: Any) -> None:
    handler, fakes, published, _ = setup
    fakes.stop_raises = RuntimeError("boom")
    handler.handle(cmd("STOP", [(ME, ORG, 2), (ME, ORG, 1)]))
    assert [(r.org_node, r.outcome, r.detail) for _, r in published] == [(2, "FAILED", "boom"),
                                                                         (1, "FAILED", "boom")]


def test_stop_through_the_real_stop_nodes_to_down(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """With nodeops' own routine: descending, one result at a time, still-UP is FAILED."""
    _, fakes, published, clock = setup
    stopped: list[int] = []

    def fake_stop(env: Any, org: str, n: int, emit: Any, **kw: Any) -> NodeResult:
        stopped.append(n)
        # every node stopped before this one has already been published
        assert [r.org_node for _, r in published] == stopped[:-1]
        return NodeResult(Outcome.STOPPED, f"pid {n}", pid=n)

    monkeypatch.setattr(nodeops, "stop_node", fake_stop)
    monkeypatch.setattr(nodeops, "node_is_up", lambda env, n: n.org_node == 2)
    monkeypatch.setattr(nodeops.time, "sleep", lambda s: None)
    ops = fakes.ops()
    ops.stop_nodes_to_down = nodeops.stop_nodes_to_down
    me = AgentIdentity(agent_id=ME, name="n", host="h", org_id=ORG, org_name="globo")
    handler = CommandHandler(me, ENV, published, logging.getLogger("t"), ops=ops, clock=clock)
    handler.handle(cmd("STOP", [(ME, ORG, 3), (ME, ORG, 2), (ME, ORG, 1)]))
    assert stopped == [3, 2, 1]
    assert published.outcomes() == [(3, "STOPPED"), (2, "FAILED"), (1, "STOPPED")]
    assert published[1][1].detail == nodeops.STILL_UP_DETAIL


# --- children --------------------------------------------------------------------------


def test_started_nodes_are_reaped_when_they_exit(setup: Any) -> None:
    handler, fakes, published, _ = setup
    waited: list[int] = []
    exited = {101}

    def waitpid(pid: int, flags: int) -> tuple[int, int]:
        waited.append(pid)
        if pid == 102:
            raise ChildProcessError
        return (pid, 0) if pid in exited else (0, 0)

    handler._waitpid = waitpid
    handler.handle(cmd("START", [(ME, ORG, 1), (ME, ORG, 2), (ME, ORG, 3)]))
    handler.reap_children()
    assert sorted(waited) == [101, 102, 103]
    waited.clear()
    handler.reap_children()
    assert waited == [103]  # never -1: only its own nodes


# --- parsing ---------------------------------------------------------------------------


def test_parse_command_from_the_avro_dict() -> None:
    value = {"eventId": "e", "createdAt": "x", "createdBy": None, "correlationId": "c-9",
             "commandType": "RESET", "command": "vl org reset", "scope": "ORG", "issuedAt": T0,
             "expiresAt": 1_763_035_230_000,
             "targets": [{"agentId": ME, "orgId": ORG, "orgNode": 3}], "options": {"includeSnapshots": "true"}}
    c = parse_command(value)
    assert c.correlation_id == "c-9" and c.command_type == "RESET" and c.targets == (Target(ME, ORG, 3),)
    assert c.expires_at == datetime(2025, 11, 13, 12, 0, 30, tzinfo=timezone.utc)
    assert c.options == {"includeSnapshots": "true"}


@pytest.mark.parametrize("broken", [
    None, "x", {"targets": "no"}, {"targets": [{"agentId": "a", "orgId": "o", "orgNode": "1"}]},
    {"targets": [], "commandType": "DANCE", "correlationId": "c", "command": "x", "issuedAt": T0,
     "expiresAt": T0},
    {"targets": [], "commandType": "START", "correlationId": "c", "command": "x", "issuedAt": "today",
     "expiresAt": T0},
])
def test_parse_command_refuses_a_malformed_record(broken: Any) -> None:
    with pytest.raises(MalformedCommand):
        parse_command(broken)
