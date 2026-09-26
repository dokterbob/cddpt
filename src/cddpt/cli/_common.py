"""Shared helpers for cddpt's CLI commands: exit codes, global CLI state,
error formatting, and small output helpers.

Kept deliberately free of any STAC/HTTP/AOI logic -- the CLI is a thin layer
over :mod:`cddpt.catalog` and :mod:`cddpt.aoi` (see cli/__init__.py).
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import typer
from rich.console import Console
from rich.logging import RichHandler

from ..errors import CddError
from ..models import CollectionInfo
from ..settings import Settings

_F = TypeVar("_F", bound=Callable[..., Any])
_err_console = Console(stderr=True)

#: Unofficial-client disclaimer, required (verbatim, per docs/PLAN.md) on
#: every top-level --help and every command's --help.
DISCLAIMER = (
    "Unofficial, independent client. Not affiliated with, endorsed by, or "
    "supported by Direção-Geral do Território."
)

# -- Exit codes ----------------------------------------------------------
#: Success.
EXIT_OK = 0
#: Generic failure -- any cddpt.errors.CddError subclass (auth/disk failures
#: below are the two exceptions carved out of this bucket).
EXIT_ERROR = 1
#: Usage error -- typer/click's own default for bad/missing CLI arguments
#: (e.g. a missing required option, an out-of-choice --format value).
EXIT_USAGE = 2
#: Reserved: authentication failure (auth lands in a later milestone; no
#: command raises this yet).
EXIT_AUTH_FAILURE = 3
#: Reserved: insufficient disk space (the downloader lands in a later
#: milestone; no command raises this yet).
EXIT_INSUFFICIENT_DISK = 4

#: Threshold (decimal bytes) above which an estimate gets the "this will
#: take a while, and that's intentional" etiquette note -- see docs/PLAN.md,
#: "Backoff & rate-limiting policy".
LARGE_ESTIMATE_BYTES = 50 * 1_000_000_000

#: Table rows rendered for `search --format table` before a "… N more"
#: footer replaces the rest -- the full result set is still counted and
#: (optionally) written to --output/streamed as ndjson/geojson regardless.
MAX_TABLE_ROWS = 200


@dataclass
class CliState:
    """Global options resolved once, in the top-level typer callback, and
    read by every subcommand (including nested ``collections`` commands)."""

    verbose: bool = False
    ca_bundle: Path | None = None


#: Set exactly once per CLI invocation, by app.py's top-level callback,
#: before any command body runs. A plain module-level global is simplest
#: here: the CLI is a single-invocation-per-process, single-threaded tool
#: (typer's nested sub-apps make plumbing this through click Context objects
#: more awkward than it's worth for that use case).
_state = CliState()


def set_state(*, verbose: bool, ca_bundle: Path | None) -> None:
    global _state
    _state = CliState(verbose=verbose, ca_bundle=ca_bundle)


def get_state() -> CliState:
    return _state


def build_settings() -> Settings:
    """Build a :class:`~cddpt.settings.Settings`, honouring ``--ca-bundle``
    when given and ``CDDPT_*`` env vars either way.

    Only passes ``ca_bundle`` through explicitly when ``--ca-bundle`` was
    actually given on the command line -- passing ``ca_bundle=None``
    unconditionally would override a ``CDDPT_CA_BUNDLE`` env var back to
    ``None`` (pydantic-settings gives explicit constructor kwargs priority
    over env vars).
    """

    state = get_state()
    if state.ca_bundle is not None:
        return Settings(ca_bundle=state.ca_bundle)
    return Settings()


def configure_logging(*, verbose: bool) -> None:
    """Route cddpt's own logging (in particular ratelimit.py's
    governor/circuit-breaker lines) to stderr.

    INFO and above when ``--verbose``, else WARNING and above -- so the
    per-wait "pausing Ns before next request" lines only show up with -v,
    but a circuit-breaker trip (logged at WARNING) is always visible.
    """

    handler = RichHandler(
        console=Console(stderr=True),
        show_time=False,
        show_path=False,
        markup=False,
        rich_tracebacks=False,
    )
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        handlers=[handler],
        format="%(message)s",
        force=True,
    )


def human_size(num_bytes: int | float | None) -> str:
    """A human-readable decimal (SI, 1000-based) byte size, e.g. ``"68.2
    GB"``. Returns ``"unknown"`` for ``None`` (never assumes 0 bytes -- see
    docs/PLAN.md's note on assets with no known ``file:size``)."""

    if num_bytes is None:
        return "unknown"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(value) < 1000.0 or unit == "PB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000.0
    return f"{value:.1f} PB"  # pragma: no cover -- unreachable, satisfies mypy


def license_reminder(collections: dict[str, CollectionInfo], collection_ids: list[str]) -> str:
    """One line reminding the user of each involved collection's *declared*
    license, verbatim -- cddpt never asserts a license itself (see
    docs/PLAN.md, "Key findings")."""

    parts = [f"{cid}={collections[cid].license!r}" for cid in collection_ids if cid in collections]
    return (
        "Licence (as declared by DGT for each collection; cddpt asserts no "
        f"licence of its own): {', '.join(parts)}"
    )


def describe_error(exc: CddError) -> str:
    """A friendly, single-line message for a :class:`~cddpt.errors.CddError`
    -- no traceback unless ``--verbose``."""

    return str(exc)


def handle_errors(func: _F) -> _F:
    """Command decorator: map any :class:`~cddpt.errors.CddError` raised by
    ``func`` to a friendly one-line rich message on stderr and
    :data:`EXIT_ERROR` -- a traceback only with ``--verbose``.

    Applied to every command function directly (rather than relying on a
    single top-level try/except around the whole app) so it is exercised the
    same way under :class:`typer.testing.CliRunner` as it is for a real
    invocation -- ``CliRunner.invoke`` only reports exit code 1 for an
    exception that escapes the command *unformatted*, otherwise.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except CddError as exc:
            if get_state().verbose:
                _err_console.print_exception()
            else:
                _err_console.print(f"[bold red]Error:[/bold red] {describe_error(exc)}")
            raise typer.Exit(code=EXIT_ERROR) from None

    return wrapper  # type: ignore[return-value]


__all__ = [
    "DISCLAIMER",
    "EXIT_AUTH_FAILURE",
    "EXIT_ERROR",
    "EXIT_INSUFFICIENT_DISK",
    "EXIT_OK",
    "EXIT_USAGE",
    "LARGE_ESTIMATE_BYTES",
    "MAX_TABLE_ROWS",
    "CliState",
    "build_settings",
    "configure_logging",
    "describe_error",
    "get_state",
    "handle_errors",
    "human_size",
    "license_reminder",
    "set_state",
]
