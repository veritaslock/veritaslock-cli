"""The agent end to end (vl-agent-spec.md §2.3, §2.4): its exit statuses, and one run
with a fake control plane and the fake Kafka: a command in, a result out, the result
consumed into results.log, and SIGTERM giving status 0."""

from __future__ import annotations

import json
import signal
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import _fake_kafka as fk
from _helpers import add_service_account, add_user
from vl.agent import EXIT_CONFIG, EXIT_FAILED, EXIT_OK, EXIT_UNAVAILABLE
from vl.agent import cp, runner
from vl.lib import api, store


@pytest.fixture(autouse=True)
def keep_signal_handlers() -> Iterator[None]:
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    yield
    for s, h in saved.items():
        signal.signal(s, h)


@pytest.fixture
def agent_account() -> store.Identity:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "x")
    return add_service_account("globo-agent", "globo", role="NODE_AGENT")


class ControlPlaneStub:
    """Answers by path: registration, heartbeats; records every request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, Any]] = []
        self.register_answers: list[Any] = []

    def __call__(self, identity: Any, idp: str, fn: Any, base: str, **kw: Any) -> Any:
        stub = self

        class Client:
            def post(self, path: str, *, json: Any = None) -> Any:
                stub.requests.append((path, json))
                if path == "/v1/agents":
                    if stub.register_answers:
                        a = stub.register_answers.pop(0)
                        if isinstance(a, Exception):
                            raise a
                        return a
                    return {"agentId": "a-1", "orgId": "org-1"}
                return None

        return fn(Client())


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> ControlPlaneStub:
    s = ControlPlaneStub()
    real = cp.ControlPlane
    monkeypatch.setattr(runner.cp, "ControlPlane", lambda env, ident: real(env, ident, call=s))
    return s


@pytest.fixture
def ck(monkeypatch: pytest.MonkeyPatch) -> Any:
    module = fk.install(monkeypatch)
    monkeypatch.setattr(runner, "import_kafka", lambda log: module)
    return module


def no_sleep(_s: float) -> None:
    pass


def stop_if_it_consumes(ck: Any) -> None:
    """A guard for the tests that expect startup to refuse: if the agent gets as far
    as consuming, SIGTERM it at its first idle poll, so run() returns 0 and the
    test fails on its status instead of hanging (Task B mutation B30)."""
    real_consumer = ck.Consumer

    def consumer(conf: dict[str, Any]) -> Any:
        c = real_consumer(conf)
        c.on_idle = lambda: signal.raise_signal(signal.SIGTERM)
        return c

    ck.Consumer = consumer


# --- exit statuses -------------------------------------------------------------------


def test_unknown_identity_exits_78() -> None:
    store.ensure_local_environment_seeded()
    assert runner.run(["--as", "nobody"], sleep=no_sleep) == EXIT_CONFIG


def test_a_user_identity_exits_78() -> None:
    add_user("ops")
    assert runner.run(["--as", "ops"], sleep=no_sleep) == EXIT_CONFIG


def test_a_store_from_a_newer_vl_exits_78(agent_account: store.Identity, isolated_store: Path) -> None:
    with sqlite3.connect(isolated_store) as conn:
        conn.execute("PRAGMA user_version = 99")
    assert runner.run(["--as", "globo-agent"], sleep=no_sleep) == EXIT_CONFIG


def test_without_the_kafka_library_exits_1(agent_account: store.Identity, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    monkeypatch.setitem(sys.modules, "confluent_kafka", None)
    assert runner.run(["--as", "globo-agent", "--host", "h"], sleep=no_sleep) == EXIT_FAILED


def test_a_token_for_another_org_exits_78(agent_account: store.Identity, ck: Any, stub: ControlPlaneStub) -> None:
    store.upsert_organization("local", "globo", "org-OTHER", "Globo", True, "x")
    assert runner.run(["--as", "globo-agent", "--host", "h"], sleep=no_sleep) == EXIT_CONFIG
    assert stub.requests == []  # never registered


def test_a_bad_agent_name_exits_78_before_registering(agent_account: store.Identity, ck: Any,
                                                      stub: ControlPlaneStub) -> None:
    assert runner.run(["--as", "globo-agent", "--host", "h", "--name", "dev box"], sleep=no_sleep) == EXIT_CONFIG
    assert stub.requests == []


def test_no_usable_verilock_exits_78(agent_account: store.Identity, ck: Any, stub: ControlPlaneStub) -> None:
    assert runner.run(["--as", "globo-agent"], sleep=no_sleep) == EXIT_CONFIG  # ~/.veritaslock is empty


def test_an_unreachable_control_plane_exits_69(agent_account: store.Identity, ck: Any,
                                               stub: ControlPlaneStub) -> None:
    stub.register_answers = [api.ApiError(0, "could not reach")] * 20
    assert runner.run(["--as", "globo-agent", "--host", "h"], sleep=no_sleep) == EXIT_UNAVAILABLE


def test_an_unreachable_broker_exits_69_and_consumes_nothing(agent_account: store.Identity, ck: Any,
                                                             stub: ControlPlaneStub) -> None:
    fk.BROKER.down = True
    assert runner.run(["--as", "globo-agent", "--host", "h"], sleep=no_sleep) == EXIT_UNAVAILABLE
    assert fk.BROKER.consumers == []


def test_a_results_subject_missing_from_the_registry_exits_78_and_consumes_nothing(
        agent_account: store.Identity, ck: Any, stub: ControlPlaneStub) -> None:
    fk.REGISTRY.subjects.clear()
    stop_if_it_consumes(ck)
    assert runner.run(["--as", "globo-agent", "--host", "h"], sleep=no_sleep) == EXIT_CONFIG
    assert fk.BROKER.consumers == []


def test_an_unreachable_schema_registry_exits_69_and_consumes_nothing(
        agent_account: store.Identity, ck: Any, stub: ControlPlaneStub) -> None:
    fk.REGISTRY.down = True
    stop_if_it_consumes(ck)
    assert runner.run(["--as", "globo-agent", "--host", "h"], sleep=no_sleep) == EXIT_UNAVAILABLE
    assert len(fk.REGISTRY.lookups) == 8  # the first try and seven retries
    assert fk.BROKER.consumers == []


# --- one run end to end ----------------------------------------------------------------


def test_a_command_in_a_result_out_and_into_results_log(agent_account: store.Identity, ck: Any,
                                                        stub: ControlPlaneStub, isolated_home: Path) -> None:
    props = isolated_home / "orgs" / "globo" / "agent" / "agent.properties"
    props.parent.mkdir(parents=True)
    props.write_text("agent.heartbeat.interval-seconds=0.05\n")
    results = isolated_home / "orgs" / "globo" / "agent" / "data" / "results.log"
    now = datetime.now(timezone.utc)
    command = {
        "eventId": "e", "createdAt": "x", "createdBy": "u-1", "correlationId": "c-42", "commandType": "START",
        "command": "vl org start", "scope": "ORG",
        "issuedAt": int(now.timestamp() * 1000), "expiresAt": int((now + timedelta(seconds=30)).timestamp() * 1000),
        "targets": [{"agentId": "a-9", "orgId": "org-1", "orgNode": 1},   # another agent's
                    {"agentId": "a-1", "orgId": "org-1", "orgNode": 7}],  # mine, not on this host
        "options": {},
    }
    state = {"sent": False, "idle": 0}

    def on_idle() -> None:
        commands = [c for c in fk.BROKER.consumers if c.group == "veritaslock-agent-a-1"]
        if not state["sent"] and commands and commands[0].topic is not None:
            fk.BROKER.append("veritaslock-agent-commands", "c-42", json.dumps(command).encode())
            state["sent"] = True
            return
        state["idle"] += 1
        heartbeats = [r for r in stub.requests if r[0] == "/v1/agents/a-1/heartbeat"]
        done = results.exists() and len(results.read_text(encoding="utf-8").splitlines()) >= 2
        if (done and heartbeats) or state["idle"] > 500:
            signal.raise_signal(signal.SIGTERM)

    real_consumer = fk.Consumer

    def consumer(conf: dict[str, Any]) -> Any:
        c = real_consumer(conf)
        if conf["group.id"] == "veritaslock-agent-a-1":
            c.on_idle = on_idle
        return c

    ck.Consumer = consumer
    code = runner.run(["--as", "globo-agent", "--host", "192.168.8.2"], sleep=no_sleep)
    assert code == EXIT_OK
    assert stub.requests[0] == ("/v1/agents", {"name": "globo-agent", "host": "192.168.8.2",
                                               "agentVersion": "0.1.0"})
    assert any(path == "/v1/agents/a-1/heartbeat" for path, _ in stub.requests)
    [(key, value)] = fk.BROKER.topics["veritaslock-agent-results"]
    record = json.loads(value)
    assert key == "c-42"
    assert (record["orgNode"], record["outcome"], record["detail"], record["agentId"]) == (
        7, "FAILED", "node not found on this host", "a-1")
    lines = results.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "time|id|command|org|node|agent|host|outcome|exit|detail"
    assert lines[1].split("|")[1:] == ["c-42", "vl org start", "globo", "7", "globo-agent", "192.168.8.2",
                                       "FAILED", "", "node not found on this host"]
    groups = {c.group for c in fk.BROKER.consumers}
    assert groups == {"veritaslock-agent-a-1", "veritaslock-agent-results-a-1"}
    assert all(c.closed for c in fk.BROKER.consumers)
    assert fk.BROKER.committed[("veritaslock-agent-a-1", "veritaslock-agent-commands")] == 1
    assert fk.BROKER.committed[("veritaslock-agent-results-a-1", "veritaslock-agent-results")] == 1
