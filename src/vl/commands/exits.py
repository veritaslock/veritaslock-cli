"""The end of a wait: the final answer, what is printed, and the exit status
(vl-agent-cli-spec.md §7.5, §7.6).

| Code | Meaning |
|---|---|
| 0 | every target succeeded (``SUCCEEDED``), or ``--no-wait`` published it |
| 1 | a hard failure: a target failed (``FAILED``), or the control plane refused |
| 2 | a usage error (Click's own) |
| 3 | incomplete: no failure, but a target did not answer or was skipped
|   | (``INCOMPLETE``), an answer still ``PENDING``, or an explicit ``--timeout`` ran out |
| 4 | the control plane could not be reached: the outcome is unknown |
| 130 | Ctrl-C: the command keeps running |

When several apply the order is 4, then 1, then 3, then 0. The status names are the
control plane's (AgentCommandStatus), whose own order (PENDING, FAILED, INCOMPLETE,
SUCCEEDED) already puts a failure before a missing answer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from vl.commands import wait as wait_mod
from vl.lib import api, store
from vl.lib.output import console, warn

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INCOMPLETE = 3
EXIT_UNREACHABLE = wait_mod.EXIT_UNREACHABLE
EXIT_INTERRUPTED = 130

STATUS_EXIT = {"SUCCEEDED": EXIT_OK, "FAILED": EXIT_FAILED, "INCOMPLETE": EXIT_INCOMPLETE,
               "PENDING": EXIT_INCOMPLETE}

# A final answer still PENDING (the control plane's clock may be a moment behind) is
# asked again every second for this many seconds.
PENDING_RETRIES = 5
PENDING_RETRY_SECONDS = 1.0


def exit_status(status: str | None, *, unreachable: bool = False) -> int:
    """The exit status for a command's overall status, by the order 4, 1, 3, 0."""
    if unreachable:
        return EXIT_UNREACHABLE
    return STATUS_EXIT.get(str(status), EXIT_INCOMPLETE)


def final_answer(get: Callable[[], dict[str, Any]], *, ask_again: bool,
                 sleep: Callable[[float], None]) -> dict[str, Any]:
    """One ``GET``; if it is still ``PENDING`` and ``ask_again``, again every second for
    up to five seconds. Raises ``api.ApiError`` as ``get`` does."""
    answer = get()
    tries = 0
    while ask_again and answer.get("status") == "PENDING" and tries < PENDING_RETRIES:
        sleep(PENDING_RETRY_SECONDS)
        tries += 1
        answer = get()
    return answer


def finish(environment: store.Environment, caller: store.Identity, correlation_id: str,
           waited: wait_mod.Waited) -> int:
    show = f"vl command show {correlation_id}"
    if waited.interrupted:
        console.print(f"Stopped waiting; the command keeps running. Command {correlation_id}. See: {show}")
        return EXIT_INTERRUPTED
    try:
        answer = final_answer(lambda: wait_mod.get_command(environment, caller, correlation_id),
                              ask_again=not waited.timed_out, sleep=wait_mod._sleep)
    except api.ApiError as exc:
        if wait_mod.unreachable(exc):
            seen = len(waited.printed)
            warn(f"The control plane could not be reached for the final answer ({exc}): the outcome could not "
                 f"be confirmed ({seen} result{'s' if seen != 1 else ''} seen above). Command {correlation_id}; "
                 f"{show} will tell.")
            return EXIT_UNREACHABLE
        raise
    except KeyboardInterrupt:
        console.print(f"Stopped waiting; the command keeps running. Command {correlation_id}. See: {show}")
        return EXIT_INTERRUPTED
    wait_mod.not_succeeded_table(environment, answer)
    console.print(wait_mod.counts_line(answer))
    if waited.timed_out and answer.get("status") == "PENDING":
        console.print("The --timeout ran out before every target answered.")
    console.print(f"See: {show}")
    return exit_status(answer.get("status"))
