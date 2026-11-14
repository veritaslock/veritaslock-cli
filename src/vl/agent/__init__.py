"""The node agent, ``vl-agent`` (vl-agent-spec.md): runs as ``python -m vl.agent``.

It serves the nodes of one org on one host: it consumes the control plane's
commands, performs them through ``vl.lib.nodeops`` (the code `vl node start`,
`stop` and `reset` run), and publishes one result per target. It imports only
from ``vl.lib``, never from ``vl.commands``.
"""

from __future__ import annotations

from vl import __version__

AGENT_VERSION = __version__

# Exit codes (vl-agent-spec.md §2.4, plus §8 item 7's 70).
EXIT_OK = 0  # stopped by SIGTERM or SIGINT
EXIT_FAILED = 1  # startup failed for another reason, or a consumer stalled twice
EXIT_UNAVAILABLE = 69  # the control plane or Kafka unreachable after the retries
EXIT_HOST_CHANGED = 70  # --print-host no longer gives the host the agent registered with
EXIT_NOPERM = 77  # authentication refused, or the account is not a NODE_AGENT
EXIT_CONFIG = 78  # configuration or identity invalid or missing


class AgentExit(Exception):
    """Stop the agent with ``code``, after logging ``message``."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
