"""cddpt's command-line interface (Milestone 3, part 1).

This is a THIN layer over :mod:`cddpt.catalog`/:mod:`cddpt.aoi`: it parses
arguments, calls the library, and formats output. No STAC/HTTP/AOI logic
lives here.

``typer``/``rich`` (and ``tqdm``, for later milestones) are only required by
the optional ``cddpt[cli]`` extra -- the base ``cddpt`` package must remain
importable without them. :func:`main` is the ``[project.scripts]`` entry
point (``cddpt = "cddpt.cli:main"``); it defers importing the actual typer
app until it is called, so ``import cddpt.cli`` itself never requires typer
either, and a missing extra prints a clear one-line message instead of a
traceback.
"""

from __future__ import annotations

import sys

#: Exit code used when a required optional dependency (the ``cddpt[cli]``
#: extra) is missing. Matches the CLI's own generic-failure exit code (see
#: cli/_common.py's EXIT_* constants) -- this is a "cddpt itself cannot run"
#: failure, not a usage error.
_EXIT_MISSING_EXTRA = 1


def main() -> None:
    """Entry point for the ``cddpt`` console script."""

    try:
        from .app import run
    except ImportError:
        print(
            "cddpt: the command-line interface requires the optional 'cli' extra. "
            "Install it with: pip install 'cddpt[cli]'",
            file=sys.stderr,
        )
        raise SystemExit(_EXIT_MISSING_EXTRA) from None

    run()


__all__ = ["main"]
