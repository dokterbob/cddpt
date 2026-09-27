"""``cddpt download`` -- AOI + collection driven, resumable, rate-limited
bulk downloads.

Thin CLI layer over :class:`cddpt.download.Downloader`: builds the exact
same AOI/collection selection ``search`` does (via ``cli/_common.py``'s
shared helpers), resolves one shared :class:`~cddpt.ratelimit.RequestGovernor`
and :class:`~cddpt.auth.base.AuthManager` for the whole run (docs/PLAN.md:
"one shared RequestGovernor per run for ALL sessions"), and drives
:meth:`~cddpt.download.Downloader.plan`/:meth:`~cddpt.download.Downloader.run`
behind a rich progress display -- no STAC/HTTP/download logic lives here.
"""

from __future__ import annotations

import threading
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from ..auth.base import AuthManager
from ..auth.form_provider import KeycloakFormAuthProvider
from ..auth.store import CredentialStore
from ..catalog import CddCatalog
from ..download import Downloader, DownloadPlan
from ..errors import AuthError, InsufficientDiskSpace
from ..models import AssetRef, DownloadOutcome, DownloadStatus
from ..naming import ByCollectionLayout, ByTileLayout, FlatLayout, Layout
from ..ratelimit import RequestGovernor
from ..settings import Settings
from . import _common

_stdout = Console()
_stderr = Console(stderr=True)

#: Above this many *remaining* (known) bytes, `download` asks for
#: confirmation unless ``--yes`` -- see docs/PLAN.md's "Backoff &
#: rate-limiting policy": a large run may genuinely take hours to days at
#: cddpt's deliberately conservative default rate.
_CONFIRM_THRESHOLD_BYTES = 5_000_000_000  # 5 GB


class LayoutChoice(str, Enum):
    """``--layout`` choices for `download`'s on-disk file layout."""

    by_collection = "by-collection"
    by_tile = "by-tile"
    flat = "flat"


def _layout_for(choice: LayoutChoice) -> Layout:
    if choice is LayoutChoice.by_collection:
        return ByCollectionLayout()
    if choice is LayoutChoice.by_tile:
        return ByTileLayout()
    return FlatLayout()


def _build_auth_manager(settings: Settings, governor: RequestGovernor) -> AuthManager:
    """Resolve the :class:`~cddpt.auth.base.AuthManager` `download`
    authenticates the CDD-host token exchange with.

    The :class:`~cddpt.auth.form_provider.KeycloakFormAuthProvider` shares
    *this run's one* ``governor`` (never builds its own) so login traffic is
    paced by the exact same budget as search/exchange/transfer traffic.

    Degrades gracefully when the system keyring is unavailable (e.g. a
    locked macOS keychain -- see docs/PLAN.md's SECRETS note) but
    ``CDDPT_USERNAME``/``CDDPT_PASSWORD`` are set: warns on stderr and
    continues with an in-memory-only session (nothing persisted) instead of
    failing outright. Without env credentials, a broken keyring is a real
    failure (there is no other way to resolve credentials) and propagates
    as :class:`~cddpt.errors.AuthError`.
    """

    store: CredentialStore | None
    try:
        candidate_store = CredentialStore()
        candidate_store.get_username()  # cheap read -- probes keyring availability
        store = candidate_store
    except AuthError as exc:
        if settings.username is not None and settings.password is not None:
            _stderr.print(
                f"[yellow]warning:[/yellow] system keyring unavailable ({exc}); "
                "continuing without persisting the session (using "
                "CDDPT_USERNAME/CDDPT_PASSWORD only)."
            )
            store = None
        else:
            raise

    provider = KeycloakFormAuthProvider(settings=settings, governor=governor, store=store)
    return AuthManager(settings=settings, provider=provider, store=store)


class _RichProgress:
    """:class:`~cddpt.download.ProgressCallback` backed by ``rich`` --
    imported only here, never by :mod:`cddpt.download` itself (see that
    module's ``ProgressCallback`` docstring: the library never imports
    ``tqdm``/``rich`` directly).

    A context manager: :meth:`Downloader.run` calls back from worker
    threads, so every method here is guarded by a lock around ``rich``'s own
    (not-guaranteed-thread-safe-for-arbitrary-mutation) task table.
    """

    def __init__(self) -> None:
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.fields[name]}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=_stdout,
        )
        self._lock = threading.Lock()
        self._task_ids: dict[str, TaskID] = {}

    def __enter__(self) -> _RichProgress:
        self._progress.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._progress.__exit__(exc_type, exc_value, traceback)

    @staticmethod
    def _key(asset: AssetRef) -> str:
        return f"{asset.collection_id}/{asset.item_id}"

    def on_start(self, asset: AssetRef, total_bytes: int | None) -> None:
        with self._lock:
            task_id = self._progress.add_task("download", name=asset.item_id, total=total_bytes)
            self._task_ids[self._key(asset)] = task_id

    def on_progress(self, asset: AssetRef, nbytes: int) -> None:
        with self._lock:
            task_id = self._task_ids.get(self._key(asset))
        if task_id is not None:
            self._progress.update(task_id, advance=nbytes)

    def on_done(self, outcome: DownloadOutcome) -> None:
        with self._lock:
            task_id = self._task_ids.get(self._key(outcome.asset))
        if task_id is None:
            return
        if outcome.status == DownloadStatus.failed:
            self._progress.update(task_id, description=f"[red]failed[/red] {outcome.asset.item_id}")
        else:
            self._progress.update(task_id, completed=max(outcome.bytes_transferred, 1))


def _print_preflight(plan: DownloadPlan) -> None:
    table = Table(title="Download preflight")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("To fetch", str(len(plan.to_download)))
    table.add_row("Already present (skipped)", str(len(plan.already_complete)))
    table.add_row("Remaining size (known)", _common.human_size(plan.remaining_known_bytes))
    table.add_row("Already-present size (known)", _common.human_size(plan.already_present_bytes))
    table.add_row("Total size (known)", _common.human_size(plan.total_known_bytes))
    if plan.unknown_size_count:
        table.add_row("Assets of unknown size", str(plan.unknown_size_count))
    table.add_row("Free disk space", _common.human_size(plan.free_disk_bytes))
    _stdout.print(table)


def _print_summary(outcomes: list[DownloadOutcome]) -> None:
    counts: dict[DownloadStatus, list[int]] = {}
    for outcome in outcomes:
        bucket = counts.setdefault(outcome.status, [0, 0])
        bucket[0] += 1
        bucket[1] += outcome.bytes_transferred

    table = Table(title="Download summary")
    table.add_column("Status")
    table.add_column("Count", justify="right")
    table.add_column("Bytes", justify="right")
    for status in DownloadStatus:
        if status not in counts:
            continue
        count, total_bytes = counts[status]
        table.add_row(status.value, str(count), _common.human_size(total_bytes))
    _stdout.print(table)

    failures = [o for o in outcomes if o.status == DownloadStatus.failed]
    if failures:
        _stderr.print(f"[bold red]{len(failures)} asset(s) failed:[/bold red]")
        for outcome in failures:
            _stderr.print(f"  {outcome.asset.item_id}: {outcome.error}")


@_common.handle_errors
def download_command(
    out: Annotated[
        Path,
        typer.Option("--out", help="Destination directory (created as needed)."),
    ],
    bbox: Annotated[
        str | None, typer.Option("--bbox", help="AOI as W,S,E,N in WGS84 decimal degrees.")
    ] = None,
    aoi: Annotated[
        Path | None,
        typer.Option(
            "--aoi",
            help="AOI from a file: .geojson/.json needs no extra; other vector "
            r"formats (shapefile, GeoPackage, ...) need the 'cddpt\[files]' extra.",
        ),
    ] = None,
    wkt: Annotated[
        str | None, typer.Option("--wkt", help="AOI as a WKT geometry string (WGS84).")
    ] = None,
    collection: Annotated[
        list[str],
        typer.Option(
            "--collection",
            help="Collection id to download from (repeatable; at least one required).",
        ),
    ] = [],  # noqa: B006 -- typer re-reads this default per invocation, never mutated
    datetime_: Annotated[
        str | None,
        typer.Option(
            "--datetime",
            help="STAC datetime filter: an instant, or 'start/end' interval "
            "('..' for an open end), RFC 3339.",
        ),
    ] = None,
    chunk_km2: Annotated[
        float | None,
        typer.Option(
            "--chunk-km2",
            help="Override the AOI chunk size (km²) used for search pagination "
            "(default: Settings.chunk_km2, 2000).",
        ),
    ] = None,
    layout: Annotated[
        LayoutChoice,
        typer.Option(
            "--layout",
            help="On-disk file layout: by-collection (default), by-tile, or flat.",
        ),
    ] = LayoutChoice.by_collection,
    concurrency: Annotated[
        int | None,
        typer.Option(
            "--concurrency",
            help="Concurrent downloads (default: Settings.concurrency, 2; hard-capped "
            "at 4 -- DGT has published no rate limits, so cddpt never exceeds that "
            "cap regardless of this option).",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Only print the preflight (what would be fetched) -- no network beyond "
            "the search itself, no download tokens spent, no auth attempted.",
        ),
    ] = False,
    overwrite: Annotated[
        bool,
        typer.Option(
            "--overwrite",
            help="Re-download even assets whose destination file already matches the "
            "declared size.",
        ),
    ] = False,
    manifest: Annotated[
        Path | None,
        typer.Option(
            "--manifest",
            help="Write a JSON manifest of every asset's outcome to this file "
            "(atomically updated after each asset; never contains a token, "
            "pre-signed URL, or cookie value).",
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", help="Skip the confirmation prompt for a large (>5 GB) remaining download."
        ),
    ] = False,
) -> None:
    the_aoi = _common.build_aoi(bbox, aoi, wkt)
    collection_ids = list(collection)
    if not collection_ids:
        raise typer.BadParameter("at least one --collection is required")

    settings = _common.build_settings()
    # One shared RequestGovernor for every session this run makes (search,
    # auth/login, the CDD-host token exchange, and the S3 transfer) -- see
    # docs/PLAN.md: "one shared RequestGovernor per run for ALL sessions".
    governor = RequestGovernor.from_settings(settings)
    catalog = CddCatalog(settings=settings, governor=governor)
    known_collections = _common.validate_collections(catalog, collection_ids)

    effective_chunk_km2 = chunk_km2 if chunk_km2 is not None else settings.chunk_km2
    chunk_count = len(the_aoi.chunks(effective_chunk_km2))
    _stderr.print(
        f"AOI area: {the_aoi.area_km2():.2f} km² -- {chunk_count} search "
        f"chunk(s) (max {effective_chunk_km2:g} km²/chunk)"
    )

    auth_manager = _build_auth_manager(settings, governor)
    progress = _RichProgress()
    downloader = Downloader(catalog, auth_manager, settings, governor=governor, progress=progress)

    assets = catalog.iter_assets(the_aoi, collection_ids, datetime=datetime_, chunk_km2=chunk_km2)
    try:
        plan = downloader.plan(assets, out, _layout_for(layout), overwrite=overwrite)
    except InsufficientDiskSpace as exc:
        _stderr.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=_common.EXIT_INSUFFICIENT_DISK) from None

    _print_preflight(plan)

    if dry_run:
        _stdout.print("[bold]Dry run:[/bold] no data was downloaded, no tokens spent.")
        _stderr.print(_common.license_reminder(known_collections, collection_ids))
        return

    if plan.to_download:
        if not yes and plan.remaining_known_bytes > _CONFIRM_THRESHOLD_BYTES:
            _stdout.print(
                Panel(
                    "Downloads are deliberately rate-limited (2 concurrent, ~2 "
                    "requests/second by default) -- this run may take hours to days. "
                    "This is intentional: DGT has published no rate limits, so cddpt "
                    "errs conservative rather than risk being blocked. See "
                    "docs/PLAN.md's rate-limiting policy.",
                    title="Note",
                    style="yellow",
                )
            )
            proceed = typer.confirm(
                f"Proceed with downloading {_common.human_size(plan.remaining_known_bytes)}?"
            )
            if not proceed:
                raise typer.Exit(code=_common.EXIT_OK)

        # Fail fast (exit 3) on bad credentials *before* spending any
        # download tokens, rather than discovering it deep inside a worker
        # thread where it would just surface as a per-asset failure.
        auth_manager.current()

    with progress:
        outcomes = downloader.run(plan, concurrency=concurrency, manifest_path=manifest)

    _print_summary(outcomes)
    _stderr.print(_common.license_reminder(known_collections, collection_ids))

    if any(o.status == DownloadStatus.failed for o in outcomes):
        raise typer.Exit(code=_common.EXIT_ERROR)


__all__ = ["LayoutChoice", "download_command"]
