"""Shared Typer/Click customization for the `vl` command tree."""

from __future__ import annotations

from typing import Any, NoReturn

import typer._click as click
from typer import BadParameter
from typer._click.exceptions import MissingParameter
from typer.core import TyperGroup


class HelpOnErrorGroup(TyperGroup):
    """Answer any incomplete or wrong invocation with ``--help``, at every level.

    Exception: a ``BadParameter`` (a value was given but is invalid — e.g. a path that doesn't exist) is re-raised.
    A ``MissingParameter`` still gets help.

    An unknown subcommand (``vl usr-acct blah``), a leaf command missing a
    required argument or option (``vl usr-acct login``), an unknown option on a
    command or a group — Click would answer these with a one-line usage hint and a
    red error box. Instead Click's one-line message (``Error: No such option:
    --force``) goes to stderr, the failing command's full ``--help`` is printed to
    stdout, and the process exits with the error's own exit code (``2``, the usual
    usage-error status).

    A bare group (``vl``, ``vl usr-acct``) is not an error: it prints its help and
    exits ``0``, exactly as if ``--help`` had been passed.

    Set as ``cls`` on every ``typer.Typer`` in the tree. ``parse_args`` catches a
    group's own ``no_args_is_help`` / option errors; ``invoke`` (on the outermost
    group) catches everything raised deeper — an unknown subcommand or a leaf
    command's argument errors — as it bubbles up.

    ``NoArgsIsHelpError`` (raised by ``no_args_is_help``) is a special case: with
    typer's rich integration, building its message calls ``ctx.get_help()``,
    which — unlike plain Click — renders straight to the console as a side
    effect rather than into the formatter it's handed. So by the time we catch
    it here, help has already been printed once; echoing ``ctx.get_help()``
    again would print it a second time. Skip the echo for that case.
    """

    def _help_and_exit(self, ctx: click.Context, err: click.exceptions.UsageError) -> NoReturn:
        if isinstance(err, click.exceptions.NoArgsIsHelpError):
            ctx.exit(0)  # help was already printed while the error was built
        click.echo(f"Error: {err.format_message()}", err=True)
        click.echo(ctx.get_help(), color=ctx.color)
        ctx.exit(err.exit_code)

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        try:
            return super().parse_args(ctx, args)
        except click.exceptions.UsageError as err:
            self._help_and_exit(err.ctx or ctx, err)

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except click.exceptions.UsageError as err:
            if isinstance(err, BadParameter) and not isinstance(err, MissingParameter):
                raise
            self._help_and_exit(err.ctx or ctx, err)
