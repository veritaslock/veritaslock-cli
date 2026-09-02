"""Shared Typer/Click customization for the `vl` command tree."""

from __future__ import annotations

from typing import Any, NoReturn

import typer._click as click
from typer.core import TyperGroup


class HelpOnErrorGroup(TyperGroup):
    """Answer any incomplete or wrong invocation with ``--help``, at every level.

    A bare group (``vl``, ``vl usr-acct``), an unknown subcommand
    (``vl usr-acct blah``), a leaf command missing a required argument
    (``vl usr-acct login``), an unknown option — Click would answer most of these
    with a one-line usage hint and a red error box. Instead the failing command's
    full ``--help`` is printed to stdout and the process exits ``0``, exactly as
    if ``--help`` had been passed.

    Set as ``cls`` on every ``typer.Typer`` in the tree. ``parse_args`` catches a
    group's own ``no_args_is_help`` / option errors; ``invoke`` (on the outermost
    group) catches everything raised deeper — an unknown subcommand or a leaf
    command's argument errors — as it bubbles up.
    """

    def _help_and_exit(self, ctx: click.Context) -> NoReturn:
        click.echo(ctx.get_help(), color=ctx.color)
        ctx.exit(0)

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        try:
            return super().parse_args(ctx, args)
        except click.exceptions.UsageError as err:
            self._help_and_exit(err.ctx or ctx)

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except click.exceptions.UsageError as err:
            self._help_and_exit(err.ctx or ctx)
