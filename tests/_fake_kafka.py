"""A stand-in for ``confluent_kafka`` (not installed in the test environment, and never
pointed at a real broker): an in-memory broker with one partition per topic, consumer
groups with committed offsets, a Schema Registry holding the services' schemas (the
copies in ``tests/fixtures/event-schema``), and "Avro" serdes that are JSON underneath
but check every record, field by field, against the schema the registry has for the
subject. Names live where the real library has them (``topic_record_subject_name_strategy``
is in ``confluent_kafka.schema_registry``, not ``confluent_kafka.serialization``)."""

from __future__ import annotations

import json
import sys
import threading
import time
import types
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


class KafkaError:
    _PARTITION_EOF = -191

    def __init__(self, code: int, text: str = "") -> None:
        self._code, self._text = code, text

    def code(self) -> int:
        return self._code

    def name(self) -> str:
        return self._text or str(self._code)

    def __str__(self) -> str:
        return self._text or str(self._code)


class Message:
    def __init__(self, topic: str, offset: int, key: str | None, value: bytes | None,
                 error: KafkaError | None = None) -> None:
        self._t, self._o, self._k, self._v, self._e = topic, offset, key, value, error

    def topic(self) -> str:
        return self._t

    def partition(self) -> int:
        return 0

    def offset(self) -> int:
        return self._o

    def key(self) -> str | None:
        return self._k

    def value(self) -> bytes | None:
        return self._v

    def error(self) -> KafkaError | None:
        return self._e


class TopicPartition:
    def __init__(self, topic: str, partition: int = 0, offset: int = -1001) -> None:
        self.topic, self.partition, self.offset = topic, partition, offset


class Broker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.topics: dict[str, list[tuple[str | None, bytes | None]]] = {}
        self.committed: dict[tuple[str, str], int] = {}
        self.consumers: list[Consumer] = []
        self.producers: list[Producer] = []
        self.down = False

    def append(self, topic: str, key: str | None, value: bytes | None) -> None:
        with self.lock:
            self.topics.setdefault(topic, []).append((key, value))


BROKER = Broker()


class Consumer:
    def __init__(self, conf: dict[str, Any]) -> None:
        self.conf = conf
        self.group = conf["group.id"]
        self.topic: str | None = None
        self.position_: int = 0
        self.closed = False
        self.commits: list[int] = []
        self.injected: list[Message] = []
        self.on_idle: Callable[[], None] | None = None
        BROKER.consumers.append(self)

    def subscribe(self, topics: list[str]) -> None:
        self.topic = topics[0]
        with BROKER.lock:
            committed = BROKER.committed.get((self.group, self.topic))
            end = len(BROKER.topics.get(self.topic, []))
        # auto.offset.reset=latest: the end, unless the group has committed
        self.position_ = committed if committed is not None else (
            end if self.conf.get("auto.offset.reset") == "latest" else 0)

    def poll(self, timeout: float) -> Message | None:
        assert not self.closed
        if self.injected:
            return self.injected.pop(0)
        assert self.topic is not None
        with BROKER.lock:
            log = BROKER.topics.get(self.topic, [])
            if self.position_ < len(log):
                key, value = log[self.position_]
                msg = Message(self.topic, self.position_, key, value)
                self.position_ += 1
                return msg
        if self.on_idle is not None:
            self.on_idle()
        time.sleep(min(timeout, 0.01))
        return None

    def commit(self, message: Message, asynchronous: bool = True) -> None:
        assert asynchronous is False
        self.commits.append(message.offset())
        with BROKER.lock:
            BROKER.committed[(self.group, message.topic())] = message.offset() + 1

    def assignment(self) -> list[TopicPartition]:
        return [TopicPartition(self.topic or "")]

    def position(self, tps: list[TopicPartition]) -> list[TopicPartition]:
        return [TopicPartition(tp.topic, 0, self.position_) for tp in tps]

    def get_watermark_offsets(self, tp: TopicPartition, timeout: float, cached: bool) -> tuple[int, int]:
        with BROKER.lock:
            return 0, len(BROKER.topics.get(tp.topic, []))

    def close(self) -> None:
        self.closed = True


class Producer:
    def __init__(self, conf: dict[str, Any]) -> None:
        self.conf = conf
        self.pending: list[tuple[str, str | None, bytes | None, Any]] = []
        self.fail_next = 0
        BROKER.producers.append(self)

    def produce(self, topic: str, key: str | None = None, value: bytes | None = None,
                on_delivery: Any = None) -> None:
        self.pending.append((topic, key, value, on_delivery))

    def flush(self, timeout: float = -1) -> int:
        pending, self.pending = self.pending, []
        for topic, key, value, cb in pending:
            if self.fail_next > 0:
                self.fail_next -= 1
                if cb is not None:
                    cb(KafkaError(-195, "_TRANSPORT"), None)
                continue
            BROKER.append(topic, key, value)
            if cb is not None:
                cb(None, Message(topic, 0, key, value))
        return 0

    def list_topics(self, topic: str | None = None, timeout: float = -1) -> object:
        if BROKER.down:
            raise RuntimeError("KafkaException: _TRANSPORT")
        return object()


class SerializationContext:
    def __init__(self, topic: str, field: str) -> None:
        self.topic, self.field = topic, field


class MessageField:
    VALUE = "value"
    KEY = "key"


def topic_record_subject_name_strategy(ctx: SerializationContext, record_name: str | None) -> str | None:
    # As the real one: no record name (a serializer built without a schema), no subject.
    return f"{ctx.topic}-{record_name}" if record_name is not None else None


SCHEMA_FIXTURES = Path(__file__).parent / "fixtures" / "event-schema"
NAMESPACE = "com.veritaslock.protocol.events.agents.v1"


def fixture_schema(name: str) -> str:
    """A copy of a services schema (tests/fixtures/event-schema/README.md)."""
    return (SCHEMA_FIXTURES / f"{name}.avsc").read_text(encoding="utf-8")


class SchemaRegistryError(Exception):
    def __init__(self, http_status_code: int, error_code: int, error_message: str) -> None:
        super().__init__(error_message)
        self.http_status_code, self.error_code, self.error_message = http_status_code, error_code, error_message


class Registry:
    """Subjects and their versions, as the control plane registered them."""

    def __init__(self) -> None:
        self.subjects: dict[str, list[str]] = {}
        self.down = False
        self.lookups: list[str] = []

    def register(self, subject: str, schema_str: str) -> None:
        self.subjects.setdefault(subject, []).append(schema_str)


REGISTRY = Registry()


class SchemaRegistryClient:
    def __init__(self, conf: dict[str, Any]) -> None:
        self.conf = conf

    def get_latest_version(self, subject: str, fmt: str | None = None) -> Any:
        REGISTRY.lookups.append(subject)
        if REGISTRY.down:
            raise ConnectionError("schema registry: connection refused")
        versions = REGISTRY.subjects.get(subject)
        if not versions:
            raise SchemaRegistryError(404, 40401, f"Subject '{subject}' not found.")
        return SimpleNamespace(schema=SimpleNamespace(schema_str=versions[-1]), version=len(versions),
                               schema_id=len(versions), subject=subject)


def check_against_schema(schema: dict[str, Any], record: dict[str, Any]) -> None:
    """The record has exactly the schema's fields, each of an acceptable type."""
    names = [f["name"] for f in schema["fields"]]
    assert sorted(record) == sorted(names), (sorted(record), sorted(names))
    for f in schema["fields"]:
        value, kind = record[f["name"]], f["type"]
        if isinstance(kind, list):  # ["null", X]
            if value is None:
                assert "null" in kind
                continue
            kind = [k for k in kind if k != "null"][0]
        if isinstance(kind, dict) and kind.get("logicalType") == "timestamp-millis":
            assert isinstance(value, datetime) and value.tzinfo is not None, f["name"]
        elif isinstance(kind, dict) and kind["type"] == "enum":
            assert value in kind["symbols"], (f["name"], value)
        elif kind == "string":
            assert isinstance(value, str), f["name"]
        elif kind == "int":
            assert isinstance(value, int) and not isinstance(value, bool), f["name"]


class AvroSerializer:
    def __init__(self, client: SchemaRegistryClient, schema_str: str | None = None,
                 conf: dict[str, Any] | None = None) -> None:
        self.client, self.conf = client, conf or {}
        self.schema = json.loads(schema_str) if schema_str is not None else None
        assert not (self.conf.get("use.latest.version") and self.conf.get("auto.register.schemas", True)), \
            "cannot enable both use.latest.version and auto.register.schemas"

    def __call__(self, obj: dict[str, Any], ctx: SerializationContext) -> bytes:
        schema = self.schema
        name = f"{schema['namespace']}.{schema['name']}" if schema is not None else None
        strategy = self.conf.get("subject.name.strategy", topic_record_subject_name_strategy)
        subject = strategy(ctx, name)
        if self.conf.get("use.latest.version") and subject is not None:
            schema = json.loads(self.client.get_latest_version(subject).schema.schema_str)
        if schema is None:  # the real library fails here too, with no schema to write
            raise TypeError("argument of type 'NoneType' is not a container or iterable")
        check_against_schema(schema, obj)
        return json.dumps(obj, default=lambda d: int(d.timestamp() * 1000)).encode()


class AvroDeserializer:
    def __init__(self, client: SchemaRegistryClient) -> None:
        self.client = client

    def __call__(self, value: bytes | None, ctx: SerializationContext) -> Any:
        assert value is not None
        if value.startswith(b"poison"):
            raise ValueError("Unknown magic byte")
        from datetime import timezone
        decoded = json.loads(value)
        for k in ("issuedAt", "expiresAt", "completedAt"):
            if isinstance(decoded, dict) and isinstance(decoded.get(k), int):
                decoded[k] = datetime.fromtimestamp(decoded[k] / 1000, tz=timezone.utc)
        return decoded


def install(monkeypatch: pytest.MonkeyPatch, librdkafka: str = "2.16.0") -> types.ModuleType:
    """Put the fake ``confluent_kafka`` (and its submodules) in ``sys.modules``."""
    global BROKER, REGISTRY
    BROKER = Broker()
    REGISTRY = Registry()
    for topic, name in (("veritaslock-agent-commands", "AgentCommand"),
                        ("veritaslock-agent-results", "AgentCommandResult")):
        REGISTRY.register(f"{topic}-{NAMESPACE}.{name}", fixture_schema(name))
    me = sys.modules[__name__]
    ck = types.ModuleType("confluent_kafka")
    ck.Consumer = Consumer  # type: ignore[attr-defined]
    ck.Producer = Producer  # type: ignore[attr-defined]
    ck.KafkaError = KafkaError  # type: ignore[attr-defined]
    ck.libversion = lambda: (librdkafka, 0)  # type: ignore[attr-defined]
    ck.version = lambda: ("2.16.0", 0)  # type: ignore[attr-defined]
    ck.broker = lambda: BROKER  # type: ignore[attr-defined]
    sr = types.ModuleType("confluent_kafka.schema_registry")
    sr.SchemaRegistryClient = SchemaRegistryClient  # type: ignore[attr-defined]
    sr.SchemaRegistryError = SchemaRegistryError  # type: ignore[attr-defined]
    sr.topic_record_subject_name_strategy = topic_record_subject_name_strategy  # type: ignore[attr-defined]
    sr.registry = lambda: REGISTRY  # type: ignore[attr-defined]
    avro = types.ModuleType("confluent_kafka.schema_registry.avro")
    avro.AvroSerializer = AvroSerializer  # type: ignore[attr-defined]
    avro.AvroDeserializer = AvroDeserializer  # type: ignore[attr-defined]
    ser = types.ModuleType("confluent_kafka.serialization")
    for name in ("SerializationContext", "MessageField"):
        setattr(ser, name, getattr(me, name))
    for name, mod in (("confluent_kafka", ck), ("confluent_kafka.schema_registry", sr),
                      ("confluent_kafka.schema_registry.avro", avro), ("confluent_kafka.serialization", ser)):
        monkeypatch.setitem(sys.modules, name, mod)
    return ck
