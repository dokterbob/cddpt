"""The actual typer application -- imported lazily by ``cli/__init__.py``'s
``main()`` so that importing ``cddpt.cli`` never requires ``typer``/``rich``
(only calling :func:`run` does).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from .. import __version__
from . import _common
from .auth import app as auth_app
from .collections import app as collections_app
from .download import download_command
from .search import search_command

app = typer.Typer(
    name="cddpt",
    add_completion=False,
    no_args_is_help=True,
)

app.add_typer(auth_app, name="auth")
app.add_typer(collections_app, name="collections")
app.command("search", help="Search a collection/AOI.")(search_command)
app.command("download", help="Download matching assets.")(download_command)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"cddpt {__version__}")
        raise typer.Exit()


@app.callback(
    help=f"Search and download data from DGT's CDD geodata portal.\n\n{_common.DISCLAIMER}"
)
def main_callback(
    verbose: Annotated[
        bool,
        typer.Option(
            "-v",
            "--verbose",
            help="Verbose logging to stderr (shows rate-governor/circuit-breaker activity).",
        ),
    ] = False,
    ca_bundle: Annotated[
        Path | None,
        typer.Option(
            "--ca-bundle",
            help="Path to a custom CA bundle for TLS verification (e.g. a corporate "
            "MITM proxy). Never disables verification -- see Settings.ca_bundle.",
        ),
    ] = None,
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show cddpt's version and exit.",
        ),
    ] = None,
) -> None:
    _common.set_state(verbose=verbose, ca_bundle=ca_bundle)
    _common.configure_logging(verbose=verbose)


def run() -> None:
    """Invoke the typer app.

    Every command is itself wrapped in :func:`cli._common.handle_errors`,
    which maps any :class:`~cddpt.errors.CddError` to a friendly one-line
    message on stderr and :data:`cli._common.EXIT_ERROR` -- so there is
    nothing left for this entry point to catch. typer/click handles usage
    errors (missing/invalid options) with :data:`cli._common.EXIT_USAGE` on
    its own.
    """

    app()


if __name__ == "__main__":  # pragma: no cover
    run()
