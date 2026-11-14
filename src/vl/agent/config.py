"""The agent's configuration (vl-agent-spec.md §3): the command line and the
optional ``$HOME/orgs/<org>/agent/agent.properties``.

Broker, Schema Registry and control plane addresses are not here: they come from
the ``environment`` row `vl` already keeps.
"""

from __future__ import annotations

import argparse
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from vl.agent import EXIT_CONFIG, AgentExit

# What the control plane accepts as an agent name (RegisterAgentRequest).
NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


@dataclass(frozen=True)
class Args:
    """The command line: ``vl-agent --as <label> [--env] [--host] [--verilock] [--name] [--log-level]``."""

    label: str
    env: str | None = None
    host: str | None = None
    verilock: Path | None = None
    name: str | None = None
    log_level: str = "INFO"


@dataclass(frozen=True)
class Settings:
    """``agent.properties`` (every key optional; the defaults are spec 2's)."""

    heartbeat_interval_seconds: float = 10.0
    # The control plane's agent.heartbeat.stale-seconds: how long a refused
    # registration from a new host waits for the old heartbeat to go stale.
    heartbeat_stale_seconds: float = 30.0
    host_check_interval_seconds: float = 60.0
    skew_warn_seconds: float = 5.0
    results_all_orgs: bool = False
    results_max_bytes: int = 10_485_760
    results_keep: int = 5
    commands_topic: str = "veritaslock-agent-commands"
    results_topic: str = "veritaslock-agent-results"


# key -> (field, parser)
_FLOAT_KEYS = {
    "agent.heartbeat.interval-seconds": "heartbeat_interval_seconds",
    "agent.heartbeat.stale-seconds": "heartbeat_stale_seconds",
    "agent.host.check-interval-seconds": "host_check_interval_seconds",
    "agent.command.skew-warn-seconds": "skew_warn_seconds",
}
_INT_KEYS = {
    "agent.results.max-bytes": "results_max_bytes",
    "agent.results.keep": "results_keep",
}
_BOOL_KEYS = {"agent.results.all-orgs": "results_all_orgs"}
_STR_KEYS = {
    "agent.topics.commands": "commands_topic",
    "agent.topics.results": "results_topic",
}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise AgentExit(EXIT_CONFIG, f"vl-agent: {message}")


def parse_args(argv: Sequence[str]) -> Args:
    p = _Parser(prog="vl-agent", description="The VeritasLock node agent (one org, this host).")
    p.add_argument("--as", dest="label", required=True, help="the NODE_AGENT service account's label")
    p.add_argument("--env", help="the environment (default: vl's default environment)")
    p.add_argument("--host", help="the host to register with (default: verilock --print-host)")
    p.add_argument("--verilock", type=Path, help="the verilock binary for --print-host")
    p.add_argument("--name", help="the agent's name (default: the service account's label)")
    p.add_argument("--log-level", default="INFO", type=str.upper, choices=LOG_LEVELS)
    ns = p.parse_args(list(argv))
    if ns.host is not None and not ns.host.strip():
        raise AgentExit(EXIT_CONFIG, "vl-agent: --host must not be empty")
    return Args(
        label=ns.label, env=ns.env, host=ns.host, verilock=ns.verilock, name=ns.name,
        log_level=ns.log_level,
    )


def agent_dir(org_name: str) -> Path:
    """``$HOME/orgs/<org>/agent``."""
    return Path.home() / "orgs" / org_name / "agent"


def results_path(org_name: str) -> Path:
    """``$HOME/orgs/<org>/agent/data/results.log``: fixed, `vl agent results` reads it."""
    return agent_dir(org_name) / "data" / "results.log"


def default_verilock() -> Path:
    """``$HOME/.veritaslock/bin/verilock``: where `vl node start` copies nodes' binaries from."""
    return Path.home() / ".veritaslock" / "bin" / "verilock"


def _bad(path: Path, lineno: int, message: str) -> AgentExit:
    return AgentExit(EXIT_CONFIG, f"{path}:{lineno}: {message}")


def load_settings(path: Path, log: logging.Logger) -> Settings:
    """Read ``agent.properties`` (``key=value``, ``#`` and ``!`` comments); a missing
    file is the defaults. A bad value is a configuration error (exit 78); an
    unknown key is a WARN, so a typo is seen but does not stop the agent."""
    settings = Settings()
    overrides: dict[str, Any] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return settings
    except OSError as exc:
        raise AgentExit(EXIT_CONFIG, f"cannot read {path}: {exc}") from exc
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line[0] in "#!":
            continue
        if "=" not in line:
            raise _bad(path, lineno, f"expected key=value, got {line!r}")
        key, value = (part.strip() for part in line.split("=", 1))
        if key in _FLOAT_KEYS:
            try:
                number = float(value)
            except ValueError:
                raise _bad(path, lineno, f"{key} must be a number, got {value!r}") from None
            if number <= 0:
                raise _bad(path, lineno, f"{key} must be positive, got {value!r}")
            overrides[_FLOAT_KEYS[key]] = number
        elif key in _INT_KEYS:
            try:
                whole = int(value)
            except ValueError:
                raise _bad(path, lineno, f"{key} must be a whole number, got {value!r}") from None
            if whole < 1:
                raise _bad(path, lineno, f"{key} must be at least 1, got {value!r}")
            overrides[_INT_KEYS[key]] = whole
        elif key in _BOOL_KEYS:
            if value.lower() not in ("true", "false"):
                raise _bad(path, lineno, f"{key} must be true or false, got {value!r}")
            overrides[_BOOL_KEYS[key]] = value.lower() == "true"
        elif key in _STR_KEYS:
            if not value:
                raise _bad(path, lineno, f"{key} must not be empty")
            overrides[_STR_KEYS[key]] = value
        else:
            log.warning("%s:%d: unknown setting %r ignored", path, lineno, key)
    return replace(settings, **overrides)
