"""Waiting for a command's results (vl-agent-cli-spec.md §7.5): a first version that
asks the control plane every two seconds until the command is no longer PENDING."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from vl.lib import api, auth, store
from vl.lib.output import console

EXIT_UNREACHABLE = 4
POLL_SECONDS = 2.0


@dataclass
class Sources:
    """Where each org's results come from (filled in by the full wait)."""

    files: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def before_request(cls, environment: store.Environment, kind: str,
                       organization: store.Organization | None) -> Sources:
        return cls()


def get_command(environment: store.Environment, caller: store.Identity, correlation_id: str) -> dict[str, Any]:
    body: dict[str, Any] = auth.authed_call(
        caller, environment.idp_base_url, lambda c: c.get(f"/v1/commands/{correlation_id}"),
        environment.cp_base_url)
    return body


def wait(environment: store.Environment, caller: store.Identity, correlation_id: str,
         targets: list[dict[str, Any]], wait_until: str, *, sources: Sources) -> int:
    console.print(f"Waiting for {len(targets)} results…")
    while True:
        try:
            answer = get_command(environment, caller, correlation_id)
        except api.ApiError:
            return EXIT_UNREACHABLE
        status = answer.get("status")
        if status != "PENDING":
            return {"SUCCEEDED": 0, "FAILED": 1}.get(str(status), 3)
        time.sleep(POLL_SECONDS)
