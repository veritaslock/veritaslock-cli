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
err_console = Console(stderr=True)

Row = dict[str, Any]


def _fmt() -> str:
    return os.environ.get("VL_OUTPUT", "table").lower()


def is_json() -> bool:
    """Whether output is JSON (``VL_OUTPUT=json``) rather than a table."""
    return _fmt() == "json"


def note(message: str) -> None:
    """Print a dim advisory line, suppressed in JSON mode so output stays parseable."""
    if _fmt() == "json":
        return
    console.print(f"[dim]{message}[/dim]")


def warn(message: str) -> None:
    """Print a warning to stderr, in every output mode: JSON on stdout stays parseable."""
    err_console.print(f"[yellow]{message}[/yellow]")


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
        table.add_row(*(_cell(row.get(col)) for col in rows[0]))
    console.print(table)


def _cell(value: Any) -> str:
    """Render one table cell. A missing / null value shows as an empty cell
    rather than the literal string ``"None"``."""
    return "" if value is None else str(value)
