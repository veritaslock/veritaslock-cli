"""The agent's Kafka side (vl-agent-spec.md §4.1, §4.4, §4.5), on the fake confluent_kafka."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import _fake_kafka as fk
from vl.agent import EXIT_FAILED, AgentExit
from vl.agent.config import Settings
from vl.agent.handler import AgentIdentity, Command, Target, TargetResult
from vl.agent.kafka import (
    KafkaIO,
    RateLimitedErrors,
    ResultPublisher,
    StallWatch,
    import_kafka,
    load_schema,
    result_row,
)
from vl.agent.resultslog import ResultsLog
from vl.agent.runner import ConsumerLoop, Stopper, handle_command_value, handle_result_value
from vl.lib import store

LOG = logging.getLogger("test.agent.kafka")
T0 = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
ENV = store.Environment("local", "http://idp", "http://cp", "http://di", "http://tok", "kafka:9092",
                        "http://sr:8081", True, "x")
ME = AgentIdentity(agent_id="a-1", name="dev-box", host="192.168.8.2", org_id="org-1", org_name="globo")


@pytest.fixture
def ck(monkeypatch: pytest.MonkeyPatch) -> Any:
    return fk.install(monkeypatch)


@pytest.fixture
def kafka(ck: Any) -> KafkaIO:
    return KafkaIO(ck, ENV, Settings(), LOG)


def command() -> Command:
    return Command("c-1", "START", "vl org start", "ORG", T0, T0 + timedelta(seconds=30),
                   (Target("a-1", "org-1", 3),), {})


# --- the library ---------------------------------------------------------------------


def test_without_confluent_kafka_the_agent_asks_for_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "confluent_kafka", None)
    with pytest.raises(AgentExit) as e:
        import_kafka(LOG)
    assert e.value.code == EXIT_FAILED and "vl[agent]" in e.value.message


def test_librdkafka_below_2_16_is_refused(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    fk.install(monkeypatch, librdkafka="2.15.9")
    with pytest.raises(AgentExit) as e:
        import_kafka(LOG)
    assert e.value.code == EXIT_FAILED and "2.16.0" in e.value.message
    fk.install(monkeypatch, librdkafka="2.16.0")
    with caplog.at_level(logging.INFO, logger="test.agent.kafka"):
        import_kafka(LOG)
    assert any("librdkafka 2.16.0" in r.message for r in caplog.records)


def test_consumer_and_producer_settings(kafka: KafkaIO) -> None:
    c = kafka.consumer("veritaslock-agent-a-1", "veritaslock-agent-commands")
    assert {k: v for k, v in c.conf.items() if k != "error_cb"} == {
        "bootstrap.servers": "kafka:9092", "group.id": "veritaslock-agent-a-1",
        "enable.auto.commit": False, "auto.offset.reset": "latest"}
    assert isinstance(c.conf["error_cb"], RateLimitedErrors)
    p = kafka.producer()
    assert p.conf["acks"] == "all" and p.conf["bootstrap.servers"] == "kafka:9092"
    ser = kafka._serializer
    assert ser.client.conf == {"url": "http://sr:8081"}
    assert ser.conf["auto.register.schemas"] is False
    assert ser.conf["subject.name.strategy"] is fk.topic_record_subject_name_strategy
    assert ser.schema["name"] == "AgentCommandResult"


def test_the_bundled_schemas_are_the_registered_ones() -> None:
    result = json.loads(load_schema("AgentCommandResult"))
    assert result["namespace"] == "com.veritaslock.protocol.events.agents.v1"
    assert [f["name"] for f in result["fields"]] == [
        "eventId", "createdAt", "agentId", "agentName", "host", "correlationId", "command", "orgId",
        "orgNode", "outcome", "detail", "exitCode", "completedAt"]
    assert json.loads(load_schema("AgentCommand"))["name"] == "AgentCommand"


# --- publishing ----------------------------------------------------------------------


def test_a_result_is_published_with_the_registered_schema(kafka: KafkaIO, ck: Any) -> None:
    producer = kafka.producer()
    publish = ResultPublisher(kafka, producer, ME, "veritaslock-agent-results", LOG, sleep=lambda s: None)
    publish(command(), TargetResult("org-1", 3, "FAILED", "exited (69)", 69, T0))
    [(key, value)] = fk.BROKER.topics["veritaslock-agent-results"]
    assert key == "c-1"
    record = json.loads(value)
    assert {k: record[k] for k in ("agentId", "agentName", "host", "correlationId", "command", "orgId",
                                   "orgNode", "outcome", "detail", "exitCode")} == {
        "agentId": "a-1", "agentName": "dev-box", "host": "192.168.8.2", "correlationId": "c-1",
        "command": "vl org start", "orgId": "org-1", "orgNode": 3, "outcome": "FAILED",
        "detail": "exited (69)", "exitCode": 69}
    assert record["completedAt"] == int(T0.timestamp() * 1000)


def test_a_failed_publish_is_retried_then_logged(kafka: KafkaIO, caplog: pytest.LogCaptureFixture) -> None:
    producer = kafka.producer()
    producer.fail_next = 2
    sleeps: list[float] = []
    publish = ResultPublisher(kafka, producer, ME, "res", LOG, sleep=sleeps.append)
    publish(command(), TargetResult("org-1", 3, "STARTED", "pid 1", None, T0))
    assert len(fk.BROKER.topics["res"]) == 1 and len(sleeps) == 2
    producer.fail_next = 5
    with caplog.at_level(logging.ERROR, logger="test.agent.kafka"):
        publish(command(), TargetResult("org-1", 3, "STARTED", "pid 1", None, T0))
    assert len(fk.BROKER.topics["res"]) == 1
    assert any("NO_ANSWER" in r.message for r in caplog.records)


def test_a_result_that_does_not_fit_the_schema_is_never_published(kafka: KafkaIO) -> None:
    producer = kafka.producer()
    publish = ResultPublisher(kafka, producer, ME, "res", LOG, sleep=lambda s: None)
    publish(command(), TargetResult("org-1", 3, "SKIPPED_UP", "x", None, T0))  # not an enum symbol
    assert "res" not in fk.BROKER.topics


# --- consumer health -----------------------------------------------------------------


def test_stall_watch() -> None:
    w = StallWatch(last_delivery=0.0)
    assert w.check(behind=True, now=59) == "ok"
    assert w.check(behind=False, now=100) == "ok"
    assert w.check(behind=True, now=61) == "restart"
    assert w.check(behind=True, now=100) == "ok"  # the clock restarts with the consumer
    assert w.check(behind=True, now=122) == "exit"  # stalled again within 5 minutes
    w2 = StallWatch(last_delivery=0.0)
    assert w2.check(True, 61) == "restart"
    w2.delivered(400)
    assert w2.check(True, 461) == "restart"  # more than 5 minutes after the first restart


def test_errors_are_logged_rate_limited(caplog: pytest.LogCaptureFixture) -> None:
    now = [0.0]
    cb = RateLimitedErrors(LOG, "commands", clock=lambda: now[0])
    with caplog.at_level(logging.ERROR, logger="test.agent.kafka"):
        for _ in range(5):
            cb(fk.KafkaError(-187, "_ALL_BROKERS_DOWN"))
        now[0] = 31
        cb(fk.KafkaError(-187, "_ALL_BROKERS_DOWN"))
        cb(fk.KafkaError(-195, "_TRANSPORT"))
    messages = [r.message for r in caplog.records]
    assert messages == ["kafka commands: _ALL_BROKERS_DOWN",
                        "kafka commands: _ALL_BROKERS_DOWN (4 more like it since)",
                        "kafka commands: _TRANSPORT"]


# --- the consumer loop ---------------------------------------------------------------


def produce(topic: str, value: Any, key: str = "k") -> None:
    fk.BROKER.append(topic, key, value if isinstance(value, bytes) else json.dumps(value).encode())


def loop_for(kafka: KafkaIO, handled: list[Any], stopper: Stopper, clock: Any = None,
             raise_on: Any = None) -> ConsumerLoop:
    def handle(v: Any) -> None:
        handled.append(v)
        if raise_on is not None and v == raise_on:
            raise RuntimeError("handler bug")
    kw = {"clock": clock} if clock is not None else {}
    return ConsumerLoop(kafka, "g", "t", handle, stopper, what="commands", **kw)


def test_messages_are_handled_in_order_then_committed(kafka: KafkaIO) -> None:
    handled: list[Any] = []
    loop = loop_for(kafka, handled, Stopper())
    produce("t", {"n": 1})
    produce("t", {"n": 2})
    for _ in range(3):
        loop.poll_once(0)
    assert handled == [{"n": 1}, {"n": 2}]
    assert loop._consumer.commits == [0, 1]


def test_the_consumer_starts_at_the_end_the_first_time(kafka: KafkaIO) -> None:
    produce("t", {"n": "old"})
    handled: list[Any] = []
    loop = loop_for(kafka, handled, Stopper())
    loop.poll_once(0)
    assert handled == []


def test_a_poison_message_is_logged_skipped_and_committed(kafka: KafkaIO, caplog: pytest.LogCaptureFixture) -> None:
    handled: list[Any] = []
    loop = loop_for(kafka, handled, Stopper())
    produce("t", b"poison")
    produce("t", {"n": 2})
    with caplog.at_level(logging.ERROR, logger="vl.agent"):
        loop.poll_once(0)
        loop.poll_once(0)
    assert handled == [{"n": 2}]
    assert loop._consumer.commits == [0, 1]
    assert any("cannot decode" in r.message for r in caplog.records)


def test_a_handler_error_is_committed_too(kafka: KafkaIO) -> None:
    handled: list[Any] = []
    loop = loop_for(kafka, handled, Stopper(), raise_on={"n": 1})
    produce("t", {"n": 1})
    loop.poll_once(0)
    assert loop._consumer.commits == [0]


def test_a_malformed_command_is_skipped(kafka: KafkaIO, caplog: pytest.LogCaptureFixture) -> None:
    class H:
        handled: list[Any] = []

        def handle(self, c: Any) -> None:
            self.handled.append(c)

        def reap_children(self) -> None:
            pass

    h = H()
    with caplog.at_level(logging.ERROR, logger="vl.agent"):
        handle_command_value(h)({"targets": "nope"})  # type: ignore[arg-type]
    assert h.handled == [] and any("not a usable AgentCommand" in r.message for r in caplog.records)


def test_a_stalled_consumer_is_recreated_then_a_second_stall_exits_1(kafka: KafkaIO) -> None:
    now = [0.0]
    stopper = Stopper()
    loop = loop_for(kafka, [], stopper, clock=lambda: now[0])
    first = loop._consumer
    produce("t", {"n": 1})
    first.position_ = 0  # behind: there is a message it does not deliver
    first.poll = lambda timeout: None  # deaf
    now[0] = 61
    loop.poll_once(0)
    assert first.closed and loop._consumer is not first
    second = loop._consumer
    second.position_ = 0
    second.poll = lambda timeout: None
    now[0] = 61 + 61
    loop.poll_once(0)
    assert stopper.is_set() and stopper.code == EXIT_FAILED


def test_a_quiet_consumer_that_is_current_is_left_alone(kafka: KafkaIO) -> None:
    now = [0.0]
    loop = loop_for(kafka, [], Stopper(), clock=lambda: now[0])
    first = loop._consumer
    now[0] = 1000
    loop.poll_once(0)
    assert loop._consumer is first and not first.closed


# --- the results file --------------------------------------------------------------------


def result_value(org_id: str = "org-1", **kw: Any) -> dict[str, Any]:
    v = {"eventId": "e", "createdAt": "x", "agentId": "a-2", "agentName": "lab-2", "host": "192.168.8.9",
         "correlationId": "c-1", "command": "vl network start", "orgId": org_id, "orgNode": 4,
         "outcome": "FAILED", "detail": "still, up", "exitCode": 69, "completedAt": T0}
    v.update(kw)
    return v


def test_own_org_results_are_written_others_skipped(tmp_path: Path) -> None:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "x")
    results = ResultsLog(tmp_path / "results.log", max_bytes=10_000, keep=2)
    handle = handle_result_value(results, "local", "org-1", all_orgs=False)
    handle(result_value())
    handle(result_value(org_id="org-2"))
    results.close()
    lines = (tmp_path / "results.log").read_text(encoding="utf-8").splitlines()
    assert lines[1:] == ["2026-11-13T12:00:00.000Z|c-1|vl network start|globo|4|lab-2|192.168.8.9|FAILED|69|still, up"]


def test_all_orgs_writes_every_result_with_the_id_when_the_name_is_unknown(tmp_path: Path) -> None:
    results = ResultsLog(tmp_path / "results.log", max_bytes=10_000, keep=2)
    handle = handle_result_value(results, "local", "org-1", all_orgs=True)
    handle(result_value(org_id="org-2", exitCode=None))
    results.close()
    fields = (tmp_path / "results.log").read_text(encoding="utf-8").splitlines()[1].split("|")
    assert fields[3] == "org-2" and fields[8] == ""


def test_a_result_that_is_not_one_is_skipped() -> None:
    assert result_row({"orgId": "o"}, "local") is None
    assert result_row("x", "local") is None
    assert result_row(result_value(completedAt="yesterday"), "local") is None
