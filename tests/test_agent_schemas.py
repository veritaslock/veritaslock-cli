"""The agent's schemas come from the Schema Registry, and it carries no copy.

The source of truth is ``veritaslock-services/event-schema``; the control plane
registers it. The copies in ``tests/fixtures/event-schema`` exist only to load a
registry in tests, and are compared here with the services repository when it is
checked out beside this one.

The tests marked ``real`` use the installed ``confluent-kafka`` (the ``vl[agent]``
extra) with its in-memory mock registry: no broker, no network. They are skipped
when the extra is not installed.
"""

from __future__ import annotations

import importlib
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import _fake_kafka as fk
from vl.agent import EXIT_CONFIG, AgentExit
from vl.agent.config import Settings
from vl.agent.handler import AgentIdentity, Command, Target, TargetResult, parse_command
from vl.agent.kafka import RESULT_FIELDS, KafkaIO, result_record, result_row, result_subject
from vl.lib import store

LOG = logging.getLogger("test.agent.schemas")
REPO = Path(__file__).resolve().parents[1]
SERVICES_SCHEMAS = REPO.parent / "veritaslock-services" / "event-schema" / "src" / "main" / "avro" / \
    "protocol" / "events"
ENV = store.Environment("local", "http://idp", "http://cp", "http://di", "http://tok", "kafka:9092",
                        "http://sr:8081", True, "x")
ME = AgentIdentity(agent_id="a-1", name="dev-box", host="192.168.8.2", org_id="org-1", org_name="globo")
T0 = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
RESULTS = "veritaslock-agent-results"
COMMANDS = "veritaslock-agent-commands"
COMMAND_SUBJECT = f"{COMMANDS}-com.veritaslock.protocol.events.agents.v1.AgentCommand"


def command() -> Command:
    return Command("c-1", "START", "vl org start", "ORG", T0, T0 + timedelta(seconds=30),
                   (Target("a-1", "org-1", 3),), {})


# --- no copy in the package --------------------------------------------------------


def test_no_schema_ships_in_the_package() -> None:
    assert list((REPO / "src").rglob("*.avsc")) == []
    assert not (REPO / "src" / "vl" / "agent" / "schemas").exists()


@pytest.mark.parametrize("name, path", [
    ("AgentCommand", "agent-commands/v1/AgentCommand.avsc"),
    ("AgentCommandResult", "agent-results/v1/AgentCommandResult.avsc"),
])
def test_the_test_copies_are_the_services_schemas(name: str, path: str) -> None:
    source = SERVICES_SCHEMAS / path
    if not source.exists():
        pytest.skip(f"veritaslock-services is not checked out at {SERVICES_SCHEMAS}")
    assert json.loads(fk.fixture_schema(name)) == json.loads(source.read_text(encoding="utf-8")), (
        f"tests/fixtures/event-schema/{name}.avsc is stale: copy {source} over it")


def test_the_agent_writes_exactly_the_result_schemas_fields() -> None:
    fields = {f["name"] for f in json.loads(fk.fixture_schema("AgentCommandResult"))["fields"]}
    assert fields == RESULT_FIELDS
    record = result_record(ME, command(), TargetResult("org-1", 3, "STARTED", "pid 1", None, T0))
    assert set(record) == RESULT_FIELDS


def test_the_results_subject_is_the_control_planes() -> None:
    assert result_subject(RESULTS) == \
        "veritaslock-agent-results-com.veritaslock.protocol.events.agents.v1.AgentCommandResult"


# --- the startup check (fake registry) ---------------------------------------------


@pytest.fixture
def ck(monkeypatch: pytest.MonkeyPatch) -> Any:
    return fk.install(monkeypatch)


def _with_fields(fields: list[dict[str, Any]]) -> str:
    schema = json.loads(fk.fixture_schema("AgentCommandResult"))
    schema["fields"] = fields
    return json.dumps(schema)


def _result_fields() -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = json.loads(fk.fixture_schema("AgentCommandResult"))["fields"]
    return fields


def test_the_registered_schema_passes_the_startup_check(ck: Any, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="test.agent.schemas"):
        KafkaIO(ck, ENV, Settings(), LOG).check_result_schema()
    assert fk.REGISTRY.lookups == [result_subject(RESULTS)]
    assert any("version 1" in r.message for r in caplog.records)


def test_an_unregistered_subject_refuses_to_start_naming_it(ck: Any) -> None:
    fk.REGISTRY.subjects.clear()
    with pytest.raises(AgentExit) as e:
        KafkaIO(ck, ENV, Settings(), LOG).check_result_schema()
    assert e.value.code == EXIT_CONFIG
    assert result_subject(RESULTS) in e.value.message and "http://sr:8081" in e.value.message


def test_a_registered_schema_that_requires_a_new_field_refuses_to_start(ck: Any) -> None:
    fk.REGISTRY.register(result_subject(RESULTS), _with_fields(
        _result_fields() + [{"name": "region", "type": "string"}]))
    with pytest.raises(AgentExit) as e:
        KafkaIO(ck, ENV, Settings(), LOG).check_result_schema()
    assert e.value.code == EXIT_CONFIG
    assert "version 2" in e.value.message and "requires region" in e.value.message


def test_a_registered_schema_without_a_field_the_agent_writes_refuses_to_start(ck: Any) -> None:
    fk.REGISTRY.register(result_subject(RESULTS), _with_fields(
        [f for f in _result_fields() if f["name"] != "host"]))
    with pytest.raises(AgentExit) as e:
        KafkaIO(ck, ENV, Settings(), LOG).check_result_schema()
    assert e.value.code == EXIT_CONFIG and "has no host" in e.value.message


def test_a_new_optional_field_is_accepted(ck: Any) -> None:
    fk.REGISTRY.register(result_subject(RESULTS), _with_fields(
        _result_fields() + [{"name": "region", "type": ["null", "string"], "default": None}]))
    KafkaIO(ck, ENV, Settings(), LOG).check_result_schema()


def test_a_configured_results_topic_has_its_own_subject(ck: Any) -> None:
    fk.REGISTRY.register(result_subject("other-results"), fk.fixture_schema("AgentCommandResult"))
    KafkaIO(ck, ENV, Settings(results_topic="other-results"), LOG).check_result_schema()
    assert fk.REGISTRY.lookups == [result_subject("other-results")]


# --- the fake has the real library's names, where the real library has them ----------

USED = {
    "confluent_kafka": ["Consumer", "Producer", "KafkaError", "libversion", "version"],
    "confluent_kafka.schema_registry": ["SchemaRegistryClient", "SchemaRegistryError"],
    "confluent_kafka.schema_registry.avro": ["AvroSerializer", "AvroDeserializer"],
    "confluent_kafka.serialization": ["SerializationContext", "MessageField"],
}


@pytest.mark.parametrize("module", sorted(USED))
def test_the_fake_and_the_real_library_agree_on_where_names_live(module: str,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    real = pytest.importorskip(module)
    fake = {}
    with monkeypatch.context() as m:
        fk.install(m)  # type: ignore[arg-type]
        fake = {name: hasattr(sys.modules[module], name) for name in USED[module]}
    for name in USED[module]:
        assert hasattr(real, name), f"{module}.{name} is not in confluent-kafka"
        assert fake[name], f"the fake lacks {module}.{name}"
    # Task A's slip: this one is in schema_registry, not serialization.
    assert not hasattr(importlib.import_module("confluent_kafka.serialization"),
                       "topic_record_subject_name_strategy")


# --- the real library, with its in-memory registry -----------------------------------


@pytest.fixture
def real() -> Any:
    pytest.importorskip("confluent_kafka.schema_registry.avro")
    for name in [n for n in sys.modules if n.startswith("confluent_kafka")]:
        assert not isinstance(getattr(sys.modules[name], "broker", None), type(lambda: 0)), "fake installed"
    return importlib.import_module("confluent_kafka")


_mock_count = 0


def mock_registry(*schemas: tuple[str, str]) -> Any:
    """A fresh in-memory registry (its own mock:// URL) with ``(subject, schema)`` registered."""
    global _mock_count
    from confluent_kafka.schema_registry import Schema, SchemaRegistryClient

    _mock_count += 1
    registry = SchemaRegistryClient.new_client({"url": f"mock://agent-schemas-{_mock_count}"})
    for subject, schema_str in schemas:
        registry.register_schema(subject, Schema(schema_str, "AVRO"))
    return registry


def registered() -> Any:
    return mock_registry((result_subject(RESULTS), fk.fixture_schema("AgentCommandResult")),
                         (COMMAND_SUBJECT, fk.fixture_schema("AgentCommand")))


def test_real_a_result_round_trips_with_no_schema_string(real: Any) -> None:
    kafka = KafkaIO(real, ENV, Settings(), LOG, registry=registered())
    kafka.check_result_schema()
    record = result_record(ME, command(), TargetResult("org-1", 3, "FAILED", "exited | 69", 69, T0))
    encoded = kafka.encode_result(record)
    assert encoded[:1] == b"\x00"  # Confluent framing: magic byte, then the registered id
    decoded = kafka.decode(RESULTS, encoded)
    assert decoded["completedAt"] == T0
    row = result_row(decoded, "local")
    assert row is not None
    assert (row.correlation_id, row.node, row.outcome, row.exit_code, row.detail, row.agent, row.host) == (
        "c-1", 3, "FAILED", 69, "exited | 69", "dev-box", "192.168.8.2")


def test_real_the_serializer_takes_the_latest_version(real: Any) -> None:
    from confluent_kafka.schema_registry import Schema

    registry = registered()
    v2 = _with_fields(_result_fields() + [{"name": "region", "type": ["null", "string"], "default": None}])
    registry.register_schema(result_subject(RESULTS), Schema(v2, "AVRO"))
    latest = registry.get_latest_version(result_subject(RESULTS))
    kafka = KafkaIO(real, ENV, Settings(), LOG, registry=registry)
    kafka.check_result_schema()
    encoded = kafka.encode_result(result_record(ME, command(), TargetResult("org-1", 3, "STARTED", "pid 7",
                                                                            None, T0)))
    assert int.from_bytes(encoded[1:5], "big") == latest.schema_id
    assert kafka.decode(RESULTS, encoded)["region"] is None


def test_real_an_unregistered_subject_is_refused_at_startup(real: Any) -> None:
    kafka = KafkaIO(real, ENV, Settings(), LOG, registry=mock_registry())
    with pytest.raises(AgentExit) as e:
        kafka.check_result_schema()
    assert e.value.code == EXIT_CONFIG and result_subject(RESULTS) in e.value.message


def test_real_a_result_that_does_not_fit_is_refused(real: Any) -> None:
    kafka = KafkaIO(real, ENV, Settings(), LOG, registry=registered())
    record = result_record(ME, command(), TargetResult("org-1", 3, "SKIPPED_UP", "x", None, T0))
    with pytest.raises(Exception):
        kafka.encode_result(record)


def test_real_a_command_as_the_control_plane_writes_it_is_read(real: Any) -> None:
    from confluent_kafka.schema_registry import topic_record_subject_name_strategy
    from confluent_kafka.schema_registry.avro import AvroSerializer
    from confluent_kafka.serialization import MessageField, SerializationContext

    registry = registered()
    # As the control plane publishes: TopicRecordNameStrategy, no auto-registration.
    control_plane = AvroSerializer(registry, fk.fixture_schema("AgentCommand"),
                                   conf={"auto.register.schemas": False, "use.latest.version": True,
                                         "subject.name.strategy": topic_record_subject_name_strategy})
    value = {
        "eventId": "e-1", "createdAt": "2026-11-14T12:00:00Z", "createdBy": "u-1", "correlationId": "c-9",
        "commandType": "STOP", "command": "vl org stop", "scope": "ORG", "issuedAt": T0,
        "expiresAt": T0 + timedelta(seconds=30),
        "targets": [{"agentId": "a-1", "orgId": "org-1", "orgNode": 2}], "options": {"includeSnapshots": "true"},
    }
    encoded = control_plane(value, SerializationContext(COMMANDS, MessageField.VALUE))
    kafka = KafkaIO(real, ENV, Settings(), LOG, registry=registry)
    cmd = parse_command(kafka.decode(COMMANDS, encoded))
    assert (cmd.correlation_id, cmd.command_type, cmd.command, cmd.targets, cmd.options) == (
        "c-9", "STOP", "vl org stop", (Target("a-1", "org-1", 2),), {"includeSnapshots": "true"})
    assert cmd.expires_at == T0 + timedelta(seconds=30)
