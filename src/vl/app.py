"""Root Typer app for `vl`.

Mounts noun-scoped sub-apps (git/kubectl style: `vl <noun> <verb>`).
"""

from __future__ import annotations

import typer

from vl.commands import env, events, identity, org, service_account, user

app = typer.Typer(
    name="vl",
    help="Unified admin CLI for VeritasLock.",
    no_args_is_help=True,
    add_completion=False,
)

# Noun groups that are implemented today.
app.add_typer(env.app, name="env")
app.add_typer(identity.app, name="identity")
app.add_typer(org.app, name="org")
app.add_typer(service_account.app, name="service-account")
app.add_typer(user.app, name="user")
app.add_typer(events.app, name="events")
# from vl.commands import team
# app.add_typer(team.app, name="team")
# from vl.commands import network
# app.add_typer(network.app, name="network")
# from vl.commands import node
# app.add_typer(node.app, name="node")


def main() -> None:
    """Console-script entry point (`vl`)."""
    app()


if __name__ == "__main__":
    main()
