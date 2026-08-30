"""`vl user` — manage VeritasLock users."""

from __future__ import annotations

from typing import Annotated

import typer

from vl.lib.config import load_config
from vl.lib.output import render

app = typer.Typer(help="Manage VeritasLock users.", no_args_is_help=True)


@app.command("add")
def add(
    username: Annotated[str, typer.Argument(help="Username to create.")],
    org: Annotated[str, typer.Option("--org", help="Organization slug.")],
) -> None:
    """Add a user to an organization."""
    cfg = load_config()
    render(
        {
            "action": "user add",
            "username": username,
            "org": org,
            "env": cfg.env,
            "api_base_url": cfg.api_base_url,
        },
        title="Would create user",
    )
    # TODO: call the IdP/Control-Plane API to create the user.
    # Implement the HTTP client in vl.lib (e.g. vl.lib.api) and call it here.


@app.command("show")
def show(
    username: Annotated[str, typer.Argument(help="Username to look up.")],
) -> None:
    """Show details for a user."""
    cfg = load_config()
    render(
        {
            "action": "user show",
            "username": username,
            "env": cfg.env,
            "api_base_url": cfg.api_base_url,
        },
        title="Would fetch user",
    )
    # TODO: call the IdP/Control-Plane API to fetch the user.
    # Implement the HTTP client in vl.lib (e.g. vl.lib.api) and call it here.
