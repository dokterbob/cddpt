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

from ..aoi import Aoi
from ..catalog import CddCatalog
from ..errors import AuthError, CddError, ConfigError
from ..models import CollectionInfo
from ..settings import Settings

_F = TypeVar("_F", bound=Callable[..., Any])

#: THE stderr console. Log records, captured Python warnings, error messages
#: and ``download``'s live progress display all go through this one object:
#: rich can only keep a live display intact for output that is printed via
#: the console the display runs on. A second ``Console(stderr=True)``
#: writes straight to the real stderr, underneath the live region, and
#: every refresh then leaves a stale copy of the progress bars behind
#: (the "same finished line repeated many times" symptom).
err_console = Console(stderr=True)
_err_console = err_console

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
#: Authentication failure -- any cddpt.errors.AuthError (including its
#: SessionExpired subclass), raised by `cddpt auth login`/`status`/`logout`
#: or by anything else that hits an auth problem. See handle_errors below.
EXIT_AUTH_FAILURE = 3
#: Insufficient free disk space for a planned download (`cddpt download`,
#: raised from :class:`~cddpt.errors.InsufficientDiskSpace`).
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
        console=err_console,
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
    # Python warnings (e.g. anything urllib3 might warn about) go through
    # logging -> the shared console too, instead of being written raw to
    # stderr underneath a live progress display.
    logging.captureWarnings(True)
    # urllib3 logs every retry at WARNING ("Retrying (Retry(total=4...))").
    # cddpt reports those waits itself (governor pause listener -> status
    # line), so only show urllib3's own lines with --verbose.
    logging.getLogger("urllib3").setLevel(logging.WARNING if verbose else logging.ERROR)


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


def parse_bbox(value: str) -> tuple[float, float, float, float]:
    """Parse a ``--bbox`` value (``W,S,E,N``) into WGS84 decimal degrees.

    Shared by ``search`` and ``download`` -- both take the exact same AOI
    selection flags (docs/PLAN.md's CLI surface).
    """

    parts = value.split(",")
    if len(parts) != 4:
        raise typer.BadParameter(f"--bbox must be W,S,E,N (got {value!r})")
    try:
        west, south, east, north = (float(p.strip()) for p in parts)
    except ValueError as exc:
        raise typer.BadParameter(f"--bbox values must be numbers (got {value!r})") from exc
    return west, south, east, north


def build_aoi(bbox: str | None, aoi_file: Path | None, wkt: str | None) -> Aoi:
    """Build an :class:`~cddpt.aoi.Aoi` from exactly one of ``--bbox``/``--aoi``/``--wkt``.

    Shared by ``search`` and ``download``.
    """

    given = [
        name
        for name, value in (("--bbox", bbox), ("--aoi", aoi_file), ("--wkt", wkt))
        if value is not None
    ]
    if len(given) == 0:
        raise typer.BadParameter("exactly one of --bbox, --aoi, or --wkt is required")
    if len(given) > 1:
        raise typer.BadParameter(
            f"--bbox, --aoi, and --wkt are mutually exclusive (got {', '.join(given)})"
        )

    if bbox is not None:
        return Aoi.from_bbox(*parse_bbox(bbox))
    if wkt is not None:
        return Aoi.from_wkt(wkt)

    assert aoi_file is not None
    if not aoi_file.is_file():
        raise typer.BadParameter(f"--aoi: no such file: {aoi_file}")
    suffix = aoi_file.suffix.lower()
    if suffix in {".geojson", ".json"}:
        return Aoi.from_geojson(aoi_file)
    # Any other vector format (shapefile, GeoPackage, ...) needs the
    # optional cddpt[files] extra -- Aoi.from_file raises a clean
    # ConfigError itself if it's missing; let it propagate (see
    # cli/app.py's generic CddError handling).
    return Aoi.from_file(aoi_file)


def validate_collections(catalog: CddCatalog, requested: list[str]) -> dict[str, CollectionInfo]:
    """Validate ``requested`` collection ids against the live
    ``/collections`` list, raising a clean :class:`~cddpt.errors.ConfigError`
    naming every valid id if any are unknown. Shared by ``search`` and
    ``download``."""

    known = {c.id: c for c in catalog.collections()}
    unknown = [cid for cid in requested if cid not in known]
    if unknown:
        raise ConfigError(
            f"cddpt: unknown collection id(s): {', '.join(unknown)}. "
            f"Valid ids: {', '.join(sorted(known))}"
        )
    return known


def describe_error(exc: CddError) -> str:
    """A friendly, single-line message for a :class:`~cddpt.errors.CddError`
    -- no traceback unless ``--verbose``."""

    return str(exc)


def handle_errors(func: _F) -> _F:
    """Command decorator: map any :class:`~cddpt.errors.CddError` raised by
    ``func`` to a friendly one-line rich message on stderr and either
    :data:`EXIT_AUTH_FAILURE` (for an :class:`~cddpt.errors.AuthError`,
    including its :class:`~cddpt.errors.SessionExpired` subclass) or
    :data:`EXIT_ERROR` (everything else) -- a traceback only with
    ``--verbose``.

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
            exit_code = EXIT_AUTH_FAILURE if isinstance(exc, AuthError) else EXIT_ERROR
            raise typer.Exit(code=exit_code) from None

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
    "build_aoi",
    "build_settings",
    "configure_logging",
    "describe_error",
    "err_console",
    "get_state",
    "handle_errors",
    "human_size",
    "license_reminder",
    "parse_bbox",
    "set_state",
    "validate_collections",
]
