"""`vl whoami` — the current target environment, default identity, and default org."""

from __future__ import annotations

from typing import Annotated

import typer

from vl.commands._shared import EnvOption, report_errors
from vl.lib import store
from vl.lib.output import render


def whoami(
    env: EnvOption = None,
    as_: Annotated[
        str | None,
        typer.Option("--as", help="Resolve as if this identity were selected."),
    ] = None,
) -> None:
    """Show the target environment, the identity commands would act as, and its org."""
    with report_errors():
        environment = store.get_environment(env)
        row = {"environment": environment.name}

        try:
            identity = store.resolve_identity(environment.name, as_)
        except store.NoResolvedIdentityError:
            row["acting_as"] = "(none — pass --as or run `vl usr-acct use <username>`)"
            row["kind"] = "-"
            row["org"] = "-"
            row["role"] = "-"
            render(row, title="whoami")
            return

        row["acting_as"] = identity.label
        row["kind"] = identity.kind
        default_org = store.default_org_for_identity(identity)
        if default_org is None:
            row["org"] = (
                "(none or ambiguous — pass --org)"
                if identity.kind == "USER"
                else "(unknown — re-cache the account)"
            )
            row["role"] = "-"
        else:
            row["org"] = default_org
            role = next(
                (
                    m.role
                    for m in store.list_org_memberships(identity.id)
                    if m.org_name == default_org
                ),
                None,
            )
            row["role"] = role or "-"
    render(row, title="whoami")
