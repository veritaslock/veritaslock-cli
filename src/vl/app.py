"""Root Typer app for `vl`.

Mounts noun-scoped sub-apps (git/kubectl style: `vl <noun> <verb>`).
"""

from __future__ import annotations

import typer

from vl.commands import events, user

app = typer.Typer(
    name="vl",
    help="Unified admin CLI for VeritasLock.",
    no_args_is_help=True,
    add_completion=False,
)

# Noun groups that are implemented today.
app.add_typer(user.app, name="user")
app.add_typer(events.app, name="events")

# Noun groups not yet built — uncomment and add the module under
# vl/commands/ as each is implemented. The mount pattern matches the two above.
# from vl.commands import svc_account
# app.add_typer(svc_account.app, name="svc-account")
# from vl.commands import org
# app.add_typer(org.app, name="org")
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
