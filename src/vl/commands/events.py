"""`vl events` — produce events into VeritasLock.

`vl events send` will eventually replace send_events.sh from veritaslock-node.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from vl.lib.config import load_config
from vl.lib.output import render

app = typer.Typer(help="Produce events into VeritasLock.", no_args_is_help=True)


@app.command("send")
def send(
    file: Annotated[
        Path,
        typer.Option(
            "--file",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Path to the event payload file to send.",
        ),
    ],
    org: Annotated[str, typer.Option("--org", help="Organization slug.")],
    count: Annotated[
        int,
        typer.Option("--count", "-n", min=1, help="Number of events to send."),
    ] = 1,
) -> None:
    """Send one or more events from a payload file."""
    cfg = load_config()
    render(
        {
            "action": "events send",
            "file": str(file),
            "org": org,
            "count": count,
            "env": cfg.env,
            "kafka_bootstrap": cfg.kafka_bootstrap,
        },
        title="Would send events",
    )
    # TODO: port the Kafka producer logic from send_events.sh into vl.lib
    # (e.g. vl.lib.kafka): construct the producer from cfg.kafka_bootstrap,
    # serialize the payload against the event-schema Avro schemas, and publish
    # `count` copies for `org`. Call that from here.
