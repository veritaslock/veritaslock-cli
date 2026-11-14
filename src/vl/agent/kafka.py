"""The agent's Kafka side (vl-agent-spec.md §4.1, §4.4, §4.5), on ``confluent-kafka``
(the ``vl[agent]`` extra), which is imported only here and only when the agent runs.

- **Commands**: topic ``veritaslock-agent-commands``, group ``veritaslock-agent-<agentId>``
  (every agent gets every command), ``auto.offset.reset=latest``, no auto-commit. One
  consumer, one command at a time; a message is committed after it was handled, and one
  that cannot be decoded is logged at ERROR and committed, never retried.
- **Results**: published to ``veritaslock-agent-results`` with ``acks=all``, key
  ``correlationId``, serialized with the registered ``AgentCommandResult`` schema
  (``TopicRecordNameStrategy``, no auto-registration, as the control plane does). The
  same topic is consumed (group ``veritaslock-agent-results-<agentId>``) into
  ``results.log``, committing after each row.
- **Consumer health**: every error the library reports is logged (rate-limited); each
  consumer is checked every 30 s, and one that is behind its partition's high watermark
  with nothing delivered for 60 s is closed and created again; a second stall within
  5 minutes stops the agent with status 1.
"""

from __future__ import annotations

import importlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from types import ModuleType
from typing import Any

from vl.agent import EXIT_FAILED, AgentExit
from vl.agent.config import Settings
from vl.agent.handler import AgentIdentity, Command, TargetResult
from vl.agent.resultslog import ResultRow
from vl.lib import store

MIN_LIBRDKAFKA = (2, 16, 0)
LIVENESS_CHECK_SECONDS = 30.0
STALL_SECONDS = 60.0
RESTALL_WINDOW_SECONDS = 300.0
ERROR_LOG_INTERVAL_SECONDS = 30.0
PUBLISH_ATTEMPTS = 3
PUBLISH_TIMEOUT_SECONDS = 10.0


def load_schema(name: str) -> str:
    """The Avro schema the control plane registered (copied from ``event-schema``)."""
    return resources.files("vl.agent.schemas").joinpath(f"{name}.avsc").read_text(encoding="utf-8")


def _version_tuple(text: str) -> tuple[int, ...]:
    parts = []
    for piece in text.split(".")[:3]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def import_kafka(log: logging.Logger) -> ModuleType:
    """``confluent_kafka``, refusing to start without it or below librdkafka 2.16.0."""
    try:
        module = importlib.import_module("confluent_kafka")
        importlib.import_module("confluent_kafka.schema_registry")
        importlib.import_module("confluent_kafka.schema_registry.avro")
    except ImportError as exc:
        raise AgentExit(
            EXIT_FAILED,
            f"the agent needs confluent-kafka with Avro support: install vl[agent] ({exc})",
        ) from exc
    librdkafka = str(module.libversion()[0])
    log.info("confluent-kafka %s, librdkafka %s", module.version()[0], librdkafka)
    if _version_tuple(librdkafka) < MIN_LIBRDKAFKA:
        raise AgentExit(
            EXIT_FAILED,
            f"librdkafka {librdkafka} is older than 2.16.0, which fixes handles left without "
            f"their broker after a brief fence: upgrade confluent-kafka to 2.16.0 or later",
        )
    return module


class RateLimitedErrors:
    """The library's ``error_cb``: every error logged, each kind at most once per interval."""

    def __init__(self, log: logging.Logger, what: str, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._log = log
        self._what = what
        self._clock = clock
        self._last: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}

    def __call__(self, err: Any) -> None:
        key = str(getattr(err, "name", lambda: "")() or err)
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < ERROR_LOG_INTERVAL_SECONDS:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return
        more = self._suppressed.pop(key, 0)
        self._last[key] = now
        suffix = f" ({more} more like it since)" if more else ""
        self._log.error("kafka %s: %s%s", self._what, err, suffix)


@dataclass
class StallWatch:
    """§4.5's liveness rule for one consumer, as a pure state machine."""

    last_delivery: float
    last_restart: float | None = None

    def delivered(self, now: float) -> None:
        self.last_delivery = now

    def check(self, behind: bool, now: float) -> str:
        """``"ok"``, ``"restart"`` (close and create the consumer again) or ``"exit"``."""
        if not behind or now - self.last_delivery < STALL_SECONDS:
            return "ok"
        if self.last_restart is not None and now - self.last_restart < RESTALL_WINDOW_SECONDS:
            return "exit"
        self.last_restart = now
        self.last_delivery = now
        return "restart"


class KafkaIO:
    """Consumers, the producer and the Avro serdes, built from the environment row."""

    def __init__(self, module: ModuleType, environment: store.Environment, settings: Settings,
                 log: logging.Logger) -> None:
        self._ck = module
        self._sr = importlib.import_module("confluent_kafka.schema_registry")
        self._avro = importlib.import_module("confluent_kafka.schema_registry.avro")
        self._serialization = importlib.import_module("confluent_kafka.serialization")
        self._env = environment
        self._settings = settings
        self._log = log
        self._registry = self._sr.SchemaRegistryClient({"url": environment.schema_reg_url})
        self._deserializer = self._avro.AvroDeserializer(self._registry)
        self._serializer = self._avro.AvroSerializer(
            self._registry,
            load_schema("AgentCommandResult"),
            conf={
                "auto.register.schemas": False,
                "use.latest.version": True,
                "subject.name.strategy": self._serialization.topic_record_subject_name_strategy,
            },
        )

    @property
    def bootstrap(self) -> str:
        return self._env.kafka_bootstrap

    def consumer_config(self, group: str) -> dict[str, Any]:
        return {
            "bootstrap.servers": self._env.kafka_bootstrap,
            "group.id": group,
            "enable.auto.commit": False,
            "auto.offset.reset": "latest",
            "error_cb": RateLimitedErrors(self._log, group),
        }

    def consumer(self, group: str, topic: str) -> Any:
        c = self._ck.Consumer(self.consumer_config(group))
        c.subscribe([topic])
        return c

    def producer(self) -> Any:
        return self._ck.Producer({
            "bootstrap.servers": self._env.kafka_bootstrap,
            "acks": "all",
            "error_cb": RateLimitedErrors(self._log, "producer"),
        })

    def check_reachable(self, timeout: float = 5.0) -> None:
        """Raise unless the broker answers a metadata request for the commands topic."""
        probe = self._ck.Producer({"bootstrap.servers": self._env.kafka_bootstrap})
        probe.list_topics(self._settings.commands_topic, timeout=timeout)

    def decode(self, topic: str, value: bytes | None) -> Any:
        ctx = self._serialization.SerializationContext(topic, self._serialization.MessageField.VALUE)
        return self._deserializer(value, ctx)

    def encode_result(self, record: dict[str, Any]) -> bytes:
        ctx = self._serialization.SerializationContext(
            self._settings.results_topic, self._serialization.MessageField.VALUE)
        encoded: bytes = self._serializer(record, ctx)
        return encoded

    def is_partition_eof(self, err: Any) -> bool:
        return bool(err.code() == self._ck.KafkaError._PARTITION_EOF)

    def behind(self, consumer: Any) -> bool:
        """Whether the consumer's position is short of a partition's high watermark."""
        for tp in consumer.assignment():
            position = consumer.position([tp])[0].offset
            _low, high = consumer.get_watermark_offsets(tp, timeout=5.0, cached=False)
            if position >= 0 and position < high:
                return True
        return False


def result_record(identity: AgentIdentity, cmd: Command, result: TargetResult) -> dict[str, Any]:
    """An ``AgentCommandResult``, as the registered schema has it."""
    return {
        "eventId": str(uuid.uuid4()),
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "agentId": identity.agent_id,
        "agentName": identity.name,
        "host": identity.host,
        "correlationId": cmd.correlation_id,
        "command": cmd.command,
        "orgId": result.org_id,
        "orgNode": result.org_node,
        "outcome": result.outcome,
        "detail": result.detail,
        "exitCode": result.exit_code,
        "completedAt": result.completed_at,
    }


class ResultPublisher:
    """Publishes each target's result at once: ``acks=all``, a few attempts, then ERROR
    (the control plane then shows ``NO_ANSWER``, which is true)."""

    def __init__(self, kafka: KafkaIO, producer: Any, identity: AgentIdentity, topic: str,
                 log: logging.Logger, *, attempts: int = PUBLISH_ATTEMPTS,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._kafka = kafka
        self._producer = producer
        self._identity = identity
        self._topic = topic
        self._log = log
        self._attempts = attempts
        self._sleep = sleep

    def __call__(self, cmd: Command, result: TargetResult) -> None:
        record = result_record(self._identity, cmd, result)
        try:
            value = self._kafka.encode_result(record)
        except Exception as exc:  # never publish a malformed result
            self._log.error("command %s: cannot serialize the result for node %d: %s",
                            cmd.correlation_id, result.org_node, exc)
            return
        for attempt in range(1, self._attempts + 1):
            errors: list[Any] = []
            delivered: list[bool] = []

            def on_delivery(err: Any, _msg: Any) -> None:
                (errors.append(err) if err is not None else delivered.append(True))

            try:
                self._producer.produce(self._topic, key=cmd.correlation_id, value=value,
                                       on_delivery=on_delivery)
                self._producer.flush(PUBLISH_TIMEOUT_SECONDS)
            except Exception as exc:
                errors.append(exc)
            if delivered and not errors:
                return
            reason = errors[0] if errors else "no delivery report in time"
            self._log.warning("command %s: publishing the result for node %d failed (attempt %d of %d): %s",
                              cmd.correlation_id, result.org_node, attempt, self._attempts, reason)
            if attempt < self._attempts:
                self._sleep(min(2.0 ** attempt, 5.0))
        self._log.error("command %s: the result for node %d (%s) was not published; the control plane "
                        "will show NO_ANSWER", cmd.correlation_id, result.org_node, result.outcome)


def org_label(environment_name: str, org_id: str) -> str:
    """The org's name from the organization cache, else its id."""
    try:
        for org in store.list_organizations(environment_name):
            if org.server_org_id == org_id:
                return org.name
    except store.StoreError:
        pass
    return org_id


def result_row(value: Any, environment_name: str) -> ResultRow | None:
    """A consumed ``AgentCommandResult`` as a results-file row; ``None`` if it is not one."""
    if not isinstance(value, dict):
        return None
    try:
        completed = value["completedAt"]
        if isinstance(completed, int):
            completed = datetime.fromtimestamp(completed / 1000, tz=timezone.utc)
        if not isinstance(completed, datetime):
            return None
        exit_code = value.get("exitCode")
        return ResultRow(
            completed_at=completed,
            correlation_id=str(value["correlationId"]),
            command=str(value["command"]),
            org=org_label(environment_name, str(value["orgId"])),
            node=int(value["orgNode"]),
            agent=str(value["agentName"]),
            host=str(value["host"]),
            outcome=str(value["outcome"]),
            exit_code=int(exit_code) if exit_code is not None else None,
            detail=str(value.get("detail") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


def describe(value: Any) -> str:
    """A short text of a decoded message, for an ERROR line."""
    try:
        return json.dumps(value, default=str)[:300]
    except (TypeError, ValueError):
        return repr(value)[:300]
