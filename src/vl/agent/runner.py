"""The agent's startup sequence and its threads (vl-agent-spec.md §2.3, §5, §7, §8 item 7).

Startup: configuration, authentication, the host, registration, then the heartbeat,
the host check and the results consumer, and last the command consumer, so that a
command never reaches an agent the control plane does not know.

Threads: the command consumer runs on the main thread (one command at a time, in
order); the heartbeat, the host check and the results consumer have one thread
each. Any of them can ask the agent to stop with a status; ``SIGTERM`` and
``SIGINT`` ask for 0. The command in hand is finished first, and the nodes are
never touched on the way out.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Any

from vl.agent import (
    AGENT_VERSION,
    EXIT_CONFIG,
    EXIT_FAILED,
    EXIT_HOST_CHANGED,
    EXIT_OK,
    AgentExit,
)
from vl.agent import config, cp, hostcheck
from vl.agent.handler import AgentIdentity, CommandHandler, MalformedCommand, parse_command
from vl.agent.kafka import (
    LIVENESS_CHECK_SECONDS,
    KafkaIO,
    ResultPublisher,
    StallWatch,
    describe,
    import_kafka,
    result_row,
)
from vl.agent.resultslog import ResultsLog
from vl.lib import api, auth, store

log = logging.getLogger("vl.agent")


class Stopper:
    """The agent's one stop request: the first status asked for wins."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self.code = EXIT_OK
        self.reason = ""

    def request(self, code: int, reason: str) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self.code, self.reason = code, reason
            self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, seconds: float) -> bool:
        return self._event.wait(seconds)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# Heartbeat (§5)
# --------------------------------------------------------------------------- #


def heartbeat_once(control: cp.ControlPlane, identity: AgentIdentity, started_at: str,
                   logger: logging.Logger) -> None:
    """One heartbeat and what its answer means; raises ``AgentExit`` only for 78."""
    try:
        control.heartbeat(identity.agent_id, AGENT_VERSION, started_at)
        return
    except auth.AuthError as exc:
        logger.error("heartbeat: cannot get a token: %s", exc)
        return
    except api.ApiError as exc:
        if exc.status_code == 404:
            logger.warning("heartbeat: the control plane does not know agent %s; registering again",
                           identity.agent_id)
            _reregister(control, identity, logger)
        elif exc.status_code == 409 and exc.context.get("reason") == "AGENT_ID_MISMATCH":
            raise AgentExit(EXIT_CONFIG, f"heartbeat refused: agent {identity.agent_id} is not this "
                                         f"service account's (AGENT_ID_MISMATCH)") from exc
        elif exc.status_code == 401:
            logger.error("heartbeat: authentication refused even with a new token: %s", exc)
        elif cp.unreachable(exc):
            logger.warning("heartbeat: no answer from the control plane (%s); still handling commands", exc)
        else:
            logger.warning("heartbeat: %s", exc)


def _reregister(control: cp.ControlPlane, identity: AgentIdentity, logger: logging.Logger) -> None:
    try:
        reg = control.register(identity.name, identity.host, AGENT_VERSION)
    except (api.ApiError, auth.AuthError) as exc:
        logger.warning("heartbeat: registering again failed (%s); will retry", exc)
        return
    if reg.agent_id != identity.agent_id:
        logger.warning("registered again with a new agent id %s (was %s); commands for the old id "
                       "are no longer this agent's", reg.agent_id, identity.agent_id)
        identity.agent_id = reg.agent_id
    else:
        logger.info("registered again as agent %s", reg.agent_id)


def heartbeat_loop(control: cp.ControlPlane, identity: AgentIdentity, started_at: str,
                   interval: float, stopper: Stopper) -> None:
    while not stopper.wait(interval):
        try:
            heartbeat_once(control, identity, started_at, log)
        except AgentExit as exc:
            stopper.request(exc.code, exc.message)
        except Exception as exc:  # a heartbeat must never kill the agent
            log.exception("heartbeat: unexpected error: %s", exc)


# --------------------------------------------------------------------------- #
# Host drift (§8 item 7)
# --------------------------------------------------------------------------- #


def host_check_once(binary: Path, registered: str, stopper: Stopper,
                    print_host: Callable[[Path], str] = hostcheck.print_host) -> None:
    try:
        current = print_host(binary)
    except hostcheck.PrintHostError as exc:
        log.warning("host check: %s; keeping %s", exc, registered)
        return
    if hostcheck.drifted(registered, current):
        message = (f"this host's address changed: registered as {registered!r}, --print-host now "
                   f"gives {current!r}; exiting so that the agent is restarted and registers again")
        log.error("%s", message)
        stopper.request(EXIT_HOST_CHANGED, message)


def host_check_loop(binary: Path, registered: str, interval: float, stopper: Stopper) -> None:
    while not stopper.wait(interval):
        try:
            host_check_once(binary, registered, stopper)
        except Exception as exc:
            log.exception("host check: unexpected error: %s", exc)


# --------------------------------------------------------------------------- #
# Consumers (§4.1, §4.4, §4.5)
# --------------------------------------------------------------------------- #


class ConsumerLoop:
    """Poll, handle, commit; with the liveness check of §4.5."""

    def __init__(self, kafka: KafkaIO, group: str, topic: str, handle: Callable[[Any], None],
                 stopper: Stopper, *, what: str, idle: Callable[[], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._kafka = kafka
        self._group = group
        self._topic = topic
        self._handle = handle
        self._stopper = stopper
        self._what = what
        self._idle = idle
        self._clock = clock
        self._consumer = kafka.consumer(group, topic)
        self._watch = StallWatch(last_delivery=clock())
        self._next_check = clock() + LIVENESS_CHECK_SECONDS

    def poll_once(self, timeout: float = 1.0) -> None:
        msg = self._consumer.poll(timeout)
        if msg is None:
            if self._idle is not None:
                self._idle()
        elif msg.error() is not None:
            if not self._kafka.is_partition_eof(msg.error()):
                log.error("kafka %s consumer: %s", self._what, msg.error())
        else:
            self._watch.delivered(self._clock())
            try:
                value = self._kafka.decode(self._topic, msg.value())
            except Exception as exc:
                log.error("%s: cannot decode the message at %s-%s@%s, skipped: %s", self._what,
                          msg.topic(), msg.partition(), msg.offset(), exc)
            else:
                try:
                    self._handle(value)
                except Exception as exc:
                    log.exception("%s: handling the message at offset %s failed, skipped: %s",
                                  self._what, msg.offset(), exc)
            try:
                self._consumer.commit(message=msg, asynchronous=False)
            except Exception as exc:
                log.error("%s: committing offset %s failed: %s", self._what, msg.offset(), exc)
        self._liveness()

    def _liveness(self) -> None:
        now = self._clock()
        if now < self._next_check:
            return
        self._next_check = now + LIVENESS_CHECK_SECONDS
        try:
            behind = self._kafka.behind(self._consumer)
        except Exception as exc:
            log.warning("%s consumer: cannot check its position: %s", self._what, exc)
            return
        action = self._watch.check(behind, now)
        if action == "restart":
            log.error("%s consumer: behind the broker with nothing delivered for 60 s; "
                      "closing it and creating it again", self._what)
            self._restart()
        elif action == "exit":
            self._stopper.request(EXIT_FAILED, f"the {self._what} consumer stalled again within "
                                               f"5 minutes of being recreated")

    def _restart(self) -> None:
        try:
            self._consumer.close()
        except Exception as exc:
            log.warning("%s consumer: close failed: %s", self._what, exc)
        self._consumer = self._kafka.consumer(self._group, self._topic)

    def run(self) -> None:
        while not self._stopper.is_set():
            self.poll_once()

    def close(self) -> None:
        try:
            self._consumer.close()
        except Exception as exc:
            log.warning("%s consumer: close failed: %s", self._what, exc)


def handle_command_value(handler: CommandHandler) -> Callable[[Any], None]:
    def handle(value: Any) -> None:
        try:
            cmd = parse_command(value)
        except MalformedCommand as exc:
            log.error("command message skipped, not a usable AgentCommand (%s): %s", exc, describe(value))
            return
        handler.handle(cmd)
        handler.reap_children()
    return handle


def handle_result_value(results: ResultsLog, environment_name: str, own_org_id: str,
                        all_orgs: bool) -> Callable[[Any], None]:
    def handle(value: Any) -> None:
        row = result_row(value, environment_name)
        if row is None:
            log.error("results message skipped, not an AgentCommandResult: %s", describe(value))
            return
        if not all_orgs and isinstance(value, dict) and value.get("orgId") != own_org_id:
            return
        results.append(row)
    return handle


# --------------------------------------------------------------------------- #
# Startup (§2.3)
# --------------------------------------------------------------------------- #


def _setup_output(level: str) -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
    root = logging.getLogger("vl.agent")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False


def _identity(args: config.Args) -> tuple[store.Environment, store.Identity, store.SvcAcct]:
    environment = store.get_environment(args.env)
    identity = store.resolve_identity(environment.name, args.label)
    if identity.kind != "SERVICE_ACCOUNT":
        raise AgentExit(EXIT_CONFIG, f"identity {args.label!r} is a {identity.kind}, not a service account")
    svc = store.get_svc_acct(identity.id)
    if svc is None:
        raise AgentExit(EXIT_CONFIG, f"identity {args.label!r} has no stored service account credentials")
    return environment, identity, svc


def run(argv: Sequence[str], *, sleep: Callable[[float], None] = time.sleep) -> int:
    started_at = _iso_now()
    stopper = Stopper()
    try:
        args = config.parse_args(argv)
    except AgentExit as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    _setup_output(args.log_level)
    log.info("vl-agent %s starting (pid %d)", AGENT_VERSION, os.getpid())

    loops: list[ConsumerLoop] = []
    results: ResultsLog | None = None
    producer: Any = None
    try:
        # 1. Configuration and identity.
        environment, identity, svc = _identity(args)
        org = store.get_organization(environment.name, svc.org_name)
        settings = config.load_settings(config.agent_dir(org.name) / "agent.properties", log)
        module = import_kafka(log)
        control = cp.ControlPlane(environment, identity)

        # 2. Authenticate.
        caller = cp.authenticate(control, log, sleep=sleep)
        if caller.org_id != org.server_org_id:
            raise AgentExit(EXIT_CONFIG, f"the token's org {caller.org_id} is not {org.name}'s "
                                         f"({org.server_org_id}) in the local cache")
        host = hostcheck.startup_host(args.host, args.verilock or config.default_verilock())
        name = args.name or identity.label
        if not config.NAME_PATTERN.match(name):
            raise AgentExit(EXIT_CONFIG, f"agent name {name!r} must be 1 to 64 letters, digits, '.', '_' "
                                         f"or '-'; pass --name")

        # 3. Register.
        reg = cp.register(control, name, host, AGENT_VERSION, log, sleep=sleep,
                          stale_seconds=settings.heartbeat_stale_seconds)
        log.info("registered as agent %s (%s) for org %s on host %s", reg.agent_id, name, org.name, host)
        me = AgentIdentity(agent_id=reg.agent_id, name=name, host=host, org_id=caller.org_id,
                           org_name=org.name)

        # Kafka, before anything consumes.
        kafka = KafkaIO(module, environment, settings, log)
        _kafka_reachable(kafka, sleep)
        _registry_ready(kafka, sleep)
        producer = kafka.producer()
        results = ResultsLog(config.results_path(org.name), max_bytes=settings.results_max_bytes,
                             keep=settings.results_keep)
        log.info("results file: %s", results.path)

        def on_signal(signum: int, _frame: FrameType | None) -> None:
            stopper.request(EXIT_OK, f"{signal.Signals(signum).name} received")

        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)

        # 4. Heartbeat, host check, results.
        threads = [threading.Thread(target=heartbeat_loop, name="heartbeat", daemon=True,
                                    args=(control, me, started_at, settings.heartbeat_interval_seconds,
                                          stopper))]
        if args.host is None:
            binary = args.verilock or config.default_verilock()
            threads.append(threading.Thread(target=host_check_loop, name="host-check", daemon=True,
                                            args=(binary, host, settings.host_check_interval_seconds,
                                                  stopper)))
        else:
            log.info("--host given: the host is not re-checked")
        results_loop = ConsumerLoop(
            kafka, f"veritaslock-agent-results-{reg.agent_id}", settings.results_topic,
            handle_result_value(results, environment.name, caller.org_id, settings.results_all_orgs),
            stopper, what="results")
        loops.append(results_loop)
        threads.append(threading.Thread(target=results_loop.run, name="results", daemon=True))

        # 5. Commands.
        handler = CommandHandler(me, environment, ResultPublisher(kafka, producer, me, settings.results_topic, log),
                                 log, skew_warn_seconds=settings.skew_warn_seconds)
        commands_loop = ConsumerLoop(
            kafka, f"veritaslock-agent-{reg.agent_id}", settings.commands_topic,
            handle_command_value(handler), stopper, what="commands", idle=handler.reap_children)
        loops.append(commands_loop)
        for t in threads:
            t.start()
        log.info("consuming commands")
        commands_loop.run()
        for t in threads:
            t.join(timeout=5.0)
    except AgentExit as exc:
        stopper.request(exc.code, exc.message)
    except store.StoreError as exc:
        stopper.request(EXIT_CONFIG, str(exc))
    except Exception as exc:
        log.exception("startup failed: %s", exc)
        stopper.request(EXIT_FAILED, f"unexpected error: {exc}")
    finally:
        for loop in loops:
            loop.close()
        if producer is not None:
            producer.flush(5.0)
        if results is not None:
            results.close()
    if stopper.code == EXIT_OK:
        log.info("stopped: %s", stopper.reason or "done")
    else:
        log.error("exiting with status %d: %s", stopper.code, stopper.reason)
    return stopper.code


def _kafka_reachable(kafka: KafkaIO, sleep: Callable[[float], None]) -> None:
    """Retry the broker for about a minute, then exit 69."""
    def probe() -> None:
        try:
            kafka.check_reachable()
        except Exception as exc:
            raise api.ApiError(0, f"kafka at {kafka.bootstrap}: {exc}") from exc
    cp.with_retries("kafka", probe, log, sleep=sleep)


def _registry_ready(kafka: KafkaIO, sleep: Callable[[float], None]) -> None:
    """The registered results schema fits (else 78); the registry retried like the broker (69)."""
    def probe() -> None:
        try:
            kafka.check_result_schema()
        except AgentExit:
            raise
        except Exception as exc:
            raise api.ApiError(0, f"schema registry: {exc}") from exc
    cp.with_retries("schema registry", probe, log, sleep=sleep)


def main() -> None:
    sys.exit(run(sys.argv[1:]))
