"""`vl history` — recent `vl` invocations recorded in the local store."""

from __future__ import annotations

from typing import Annotated

import typer

from vl.commands._shared import report_errors
from vl.lib import store
from vl.lib.output import render


def history(
    limit: Annotated[
        int,
        typer.Option("-n", "--limit", min=1, help="How many recent commands to show."),
    ] = 25,
) -> None:
    """Show the most recent `vl` commands (oldest first, like shell history)."""
    with report_errors():
        entries = store.list_command_history(limit)

    rows = [
        {
            "when": entry.ran_at[:19].replace("T", " "),
            "command": entry.command,
        }
        for entry in entries
    ]
    render(rows, title="history")
