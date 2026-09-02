"""Root Typer app for `vl`.

Mounts noun-scoped sub-apps (git/kubectl style: `vl <noun> <verb>`).
"""

from __future__ import annotations

import sys

import typer

from vl.commands import env, org, svc_acct, team, usr_acct
from vl.commands.history import history
from vl.commands.whoami import whoami
from vl.lib import store

app = typer.Typer(
    name="vl",
    help="Unified admin CLI for VeritasLock.",
    no_args_is_help=True,
    add_completion=False,
)

app.add_typer(env.app, name="env")
app.add_typer(org.app, name="org")
app.add_typer(usr_acct.app, name="usr-acct")
app.add_typer(svc_acct.app, name="svc-acct")
app.add_typer(team.app, name="team")
app.command("whoami")(whoami)
app.command("history")(history)


def main() -> None:
    """Console-script entry point (`vl`)."""
    argv = sys.argv[1:]
    # `vl history` itself is not recorded — it would just push the entry the user
    # is trying to read off the bottom of the window.
    if argv and argv[0] != "history":
        store.record_command(argv)
    app()


if __name__ == "__main__":
    main()
