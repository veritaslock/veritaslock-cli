"""Shared output rendering for `vl` commands.

Commands build a plain dict (or list of dicts) and hand it to `render()`.
Output format is controlled by the VL_OUTPUT env var: "table" (default) or
"json". Keeping this in one place means every command group renders the same.
"""

from __future__ import annotations

import json
import os
from typing import Any

from rich.console import Console
from rich.table import Table

console = Console()

Row = dict[str, Any]


def _fmt() -> str:
    return os.environ.get("VL_OUTPUT", "table").lower()


def render(data: Row | list[Row], *, title: str | None = None) -> None:
    """Render a row or list of rows as a table or JSON."""
    rows = data if isinstance(data, list) else [data]

    if _fmt() == "json":
        console.print_json(json.dumps(data))
        return

    if not rows:
        console.print("[dim](no results)[/dim]")
        return

    table = Table(title=title)
    for column in rows[0]:
        table.add_column(str(column))
    for row in rows:
        table.add_row(*(str(row.get(col, "")) for col in rows[0]))
    console.print(table)
