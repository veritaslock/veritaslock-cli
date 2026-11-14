"""The end of a wait: the final answer, what is printed, and the exit status
(vl-agent-cli-spec.md §7.5, §7.6)."""

from __future__ import annotations

from vl.commands import wait as wait_mod
from vl.lib import api, store
from vl.lib.output import console

STATUS_EXIT = {"SUCCEEDED": 0, "FAILED": 1, "INCOMPLETE": 3, "PENDING": 3}


def finish(environment: store.Environment, caller: store.Identity, correlation_id: str,
           waited: wait_mod.Waited) -> int:
    try:
        answer = wait_mod.get_command(environment, caller, correlation_id)
    except api.ApiError as exc:
        console.print(f"The outcome could not be confirmed ({exc}). Command {correlation_id}: "
                      f"vl command show {correlation_id} will tell.")
        return wait_mod.EXIT_UNREACHABLE
    wait_mod.not_succeeded_table(environment, answer)
    console.print(wait_mod.counts_line(answer))
    console.print(f"See: vl command show {correlation_id}")
    return STATUS_EXIT.get(str(answer.get("status")), 3)
