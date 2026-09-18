"""`vl env` — manage environment definitions in the local `vl` store.

All commands are local-only: they touch `store.db` and never call any API, so no
authentication or bootstrap identity is required to get to a working environment.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

import typer

from vl.lib import store
from vl.lib.cli import HelpOnErrorGroup
from vl.lib.output import console, render

app = typer.Typer(
    help="Manage VeritasLock environments (local store only).",
    no_args_is_help=True,
    cls=HelpOnErrorGroup,
)


@contextmanager
def _report_errors() -> Iterator[None]:
    """Turn a `StoreError` into a clean message + non-zero exit."""
    try:
        yield
    except store.StoreError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc


def _env_row(env: store.Environment) -> dict[str, str]:
    return {
        "name": env.name,
        "idp_base_url": env.idp_base_url,
        "cp_base_url": env.cp_base_url,
        "di_base_url": env.di_base_url,
        "kafka_bootstrap": env.kafka_bootstrap or "",
        "default": "*" if env.is_default else "",
    }


@app.command("add")
def add(
    name: Annotated[str, typer.Argument(help="Environment name, e.g. 'dev'.")],
    idp_url: Annotated[
        str, typer.Option("--idp-url", help="Identity-provider base URL.")
    ],
    cp_url: Annotated[
        str, typer.Option("--cp-url", help="Control-plane base URL.")
    ],
    di_url: Annotated[
        str, typer.Option("--di-url", help="Data-ingestion base URL.")
    ],
    token_url: Annotated[
        str, typer.Option("--token-url", help="Token URL.")
    ],
    schema_reg_url: Annotated[
        str, typer.Option("--schema-reg-url", help="Schema registry URL.")
    ],
    kafka: Annotated[
        str, typer.Option("--kafka", help="Kafka bootstrap servers.")
    ]
) -> None:
    """Add a new environment (does not make it the default)."""
    with _report_errors():
        env = store.add_environment(name, idp_url, cp_url, di_url, token_url, schema_reg_url, kafka)
    render(_env_row(env), title="Environment added")


@app.command("list")
def list_() -> None:
    """List all environments."""
    with _report_errors():
        envs = store.list_environments()
    render([_env_row(env) for env in envs], title="Environments")


@app.command("show")
def show(
    name: Annotated[str, typer.Argument(help="Environment name.")],
) -> None:
    """Show a single environment."""
    with _report_errors():
        env = store.get_environment(name)
    render(_env_row(env), title=f"Environment: {name}")


@app.command("use")
def use(
    name: Annotated[str, typer.Argument(help="Environment name.")],
) -> None:
    """Set the default environment."""
    with _report_errors():
        store.set_default_environment(name)
    console.print(f"Default environment is now [bold]{name}[/bold].")


@app.command("update")
def update(
    name: Annotated[str, typer.Argument(help="Environment name.")],
    idp_url: Annotated[
        str | None, typer.Option("--idp-url", help="New identity-provider base URL.")
    ] = None,
    cp_url: Annotated[
        str | None, typer.Option("--cp-url", help="New control-plane base URL.")
    ] = None,
    di_url: Annotated[
        str | None, typer.Option("--di-url", help="New data-ingestion base URL.")
    ] = None,
    token_url: Annotated[
        str | None, typer.Option("--token-url", help="New token URL.")
    ] = None,
    schema_reg_url: Annotated[
        str | None, typer.Option("--schema-reg-url", help="New schema registry URL.")
    ] = None,
    kafka: Annotated[
        str | None, typer.Option("--kafka", help="New Kafka bootstrap servers.")
    ] = None,
) -> None:
    """Update one or more fields of an environment."""
    if idp_url is None and cp_url is None and di_url is None and kafka is None:
        console.print(
            "[red]Error:[/red] nothing to update — supply at least one of "
            "--idp-url / --cp-url / --di-url / --kafka."
        )
        raise typer.Exit(1)
    with _report_errors():
        env = store.update_environment(name, idp_url, cp_url, di_url, token_url, schema_reg_url, kafka)
    render(_env_row(env), title="Environment updated")


@app.command("delete")
def delete(
    name: Annotated[str, typer.Argument(help="Environment name.")],
) -> None:
    """Delete an environment (refused if it is the default)."""
    with _report_errors():
        store.delete_environment(name)
    console.print(f"Environment [bold]{name}[/bold] deleted.")
