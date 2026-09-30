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

import getpass
import threading
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Annotated, Any

import typer
from pydantic import SecretStr
from rich.console import Console, RenderableType
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
from rich.text import Text

from ..auth.base import AuthManager, AuthProvider
from ..auth.form_provider import KeycloakFormAuthProvider
from ..auth.manual_provider import ManualCookieAuthProvider
from ..auth.store import CredentialStore
from ..catalog import CddCatalog
from ..download import Downloader, DownloadPlan
from ..errors import AuthError, InsufficientDiskSpace
from ..models import AssetRef, DownloadOutcome, DownloadStatus
from ..naming import ByCollectionLayout, ByTileLayout, FlatLayout, Layout
from ..ratelimit import GovernorPause, GovernorStats, PauseReason, RequestGovernor
from ..settings import Settings
from . import _common

_stdout = Console()
#: Shared with logging and the live progress display -- see
#: ``_common.err_console`` for why it must be one and the same console.
_stderr = _common.err_console

#: Waits shorter than this are ordinary pacing and are not worth a status
#: line; anything longer is always shown (with a countdown).
_PAUSE_DISPLAY_THRESHOLD_SECONDS = 2.0

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


def _build_auth_manager(
    settings: Settings, governor: RequestGovernor, *, cookie: SecretStr | None = None
) -> AuthManager:
    """Resolve the :class:`~cddpt.auth.base.AuthManager` `download`
    authenticates the CDD-host token exchange with.

    The CDD session itself is never persisted -- see
    ``cddpt.auth.base``'s module docstring -- so there is nothing here to
    degrade gracefully around beyond credential *resolution*: a manually
    supplied ``cookie`` (from ``--cookie`` or ``CDDPT_SESSION_COOKIE``) wins
    outright; otherwise this falls back to username/password.

    The :class:`~cddpt.auth.form_provider.KeycloakFormAuthProvider` shares
    *this run's one* ``governor`` (never builds its own) so login traffic is
    paced by the exact same budget as search/exchange/transfer traffic.

    ``CDDPT_USERNAME``/``CDDPT_PASSWORD`` work even when the system keyring
    is unavailable (e.g. a locked macOS keychain -- see docs/PLAN.md's
    SECRETS note): the keyring is only consulted when env credentials are
    absent, so it's never touched in that case. Without env credentials, a
    broken keyring is a real failure (there is no other way to resolve
    credentials) and propagates as :class:`~cddpt.errors.AuthError`.
    """

    manual_cookie = cookie if cookie is not None else settings.session_cookie
    if manual_cookie is not None:
        provider: AuthProvider = ManualCookieAuthProvider(cookie_value=manual_cookie)
        return AuthManager(settings=settings, provider=provider)

    store: CredentialStore | None = None
    if settings.username is None or settings.password is None:
        store = CredentialStore()
        store.get_username()  # cheap read -- raises AuthError if keyring is unusable

    provider = KeycloakFormAuthProvider(settings=settings, governor=governor, store=store)
    return AuthManager(settings=settings, provider=provider)


def _format_remaining(seconds: float) -> str:
    seconds = max(0, round(seconds))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}:{secs:02d}" if minutes else f"{secs} s"


#: Which pause to show when several threads are waiting for different
#: reasons at once: the one that holds up the whole run first.
_PAUSE_PRIORITY = {
    PauseReason.circuit_breaker: 0,
    PauseReason.retry_after: 1,
    PauseReason.retry_backoff: 2,
}


class _PauseStatus:
    """A :class:`~cddpt.ratelimit.PauseListener` that turns governor pauses
    into one status line above the progress bars, with a live countdown
    (re-rendered on every display refresh)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[int, GovernorPause] = {}

    def on_pause(self, pause: GovernorPause) -> None:
        if pause.seconds < _PAUSE_DISPLAY_THRESHOLD_SECONDS:
            return
        with self._lock:
            self._active[pause.pause_id] = pause

    def on_resume(self, pause: GovernorPause) -> None:
        with self._lock:
            self._active.pop(pause.pause_id, None)

    def renderable(self) -> RenderableType | None:
        with self._lock:
            pauses = list(self._active.values())
        if not pauses:
            return None
        pause = min(
            pauses,
            key=lambda p: (_PAUSE_PRIORITY[p.reason], -p.resume_at.timestamp()),
        )
        remaining = (pause.resume_at - datetime.now(timezone.utc)).total_seconds()
        until = pause.resume_at.astimezone().strftime("%H:%M:%S")
        if pause.reason is PauseReason.circuit_breaker:
            message = (
                "Circuit breaker open (too many HTTP 429/503): all requests paused "
                f"until {until} (in {_format_remaining(remaining)})"
            )
        elif pause.reason is PauseReason.retry_after:
            message = (
                "Rate limited by the server (Retry-After): resuming in "
                f"{_format_remaining(remaining)} ({until})"
            )
        else:
            cause = f"HTTP {pause.status}" if pause.status is not None else "a network error"
            message = f"Retrying after {cause}: waiting {_format_remaining(remaining)}"
        return Text(f"⏸  {message}", style="yellow")


class _StatusProgress(Progress):
    """A :class:`rich.progress.Progress` with an optional status line (see
    :class:`_PauseStatus`) rendered above the task table."""

    def __init__(self, *columns: Any, status: _PauseStatus, **kwargs: Any) -> None:
        self._status = status
        super().__init__(*columns, **kwargs)

    def get_renderables(self) -> Iterable[RenderableType]:
        status = self._status.renderable()
        if status is not None:
            yield status
        yield self.make_tasks_table(self.tasks)


class _RichProgress:
    """:class:`~cddpt.download.ProgressCallback` backed by ``rich`` --
    imported only here, never by :mod:`cddpt.download` itself (see that
    module's ``ProgressCallback`` docstring: the library never imports
    ``tqdm``/``rich`` directly).

    Shows one overall bar plus one bar per asset *in flight*: an asset's bar
    is removed as soon as it finishes (a failure is printed as a line above
    the display instead), so the live region stays a few lines tall however
    many assets a run has -- a display taller than the terminal can't be
    redrawn in place. A status line above the bars says why, and until when,
    requests are paused whenever the shared governor holds them back.

    A context manager: :meth:`Downloader.run` calls back from worker
    threads, so every method here is guarded by a lock around ``rich``'s own
    (not-guaranteed-thread-safe-for-arbitrary-mutation) task table.
    """

    def __init__(self, console: Console | None = None) -> None:
        self._console = console if console is not None else _stderr
        self.pause_status = _PauseStatus()
        self._progress = _StatusProgress(
            SpinnerColumn(),
            TextColumn("[bold]{task.fields[name]}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=self._console,
            status=self.pause_status,
        )
        self._lock = threading.Lock()
        self._task_ids: dict[str, TaskID] = {}
        self._overall: TaskID | None = None
        self._files_total = 0
        self._files_done = 0

    def start_run(self, files: int, total_bytes: int | None) -> None:
        """Add the overall bar: ``files`` assets to fetch, ``total_bytes``
        (``None`` if not all sizes are known) in total."""

        with self._lock:
            self._files_total = files
            self._files_done = 0
            self._overall = self._progress.add_task(
                "total", name=self._overall_name(), total=total_bytes
            )

    def _overall_name(self) -> str:
        return f"Total ({self._files_done}/{self._files_total} files)"

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

    def on_skipped(self, outcomes: Sequence[DownloadOutcome]) -> None:
        """Preflight already reports these files; no transfer bars are needed."""

    def on_progress(self, asset: AssetRef, nbytes: int) -> None:
        with self._lock:
            task_id = self._task_ids.get(self._key(asset))
            overall = self._overall
        if task_id is not None:
            self._progress.update(task_id, advance=nbytes)
        if overall is not None:
            self._progress.update(overall, advance=nbytes)

    def on_done(self, outcome: DownloadOutcome) -> None:
        with self._lock:
            task_id = self._task_ids.pop(self._key(outcome.asset), None)
            if outcome.status != DownloadStatus.skipped:
                self._files_done += 1
            overall = self._overall
            overall_name = self._overall_name()
        if task_id is not None:
            self._progress.remove_task(task_id)
        if overall is not None:
            self._progress.update(overall, name=overall_name)
        if outcome.status == DownloadStatus.failed:
            self._console.print(
                f"[red]failed[/red] {outcome.asset.item_id}: {outcome.error}",
                markup=True,
                highlight=False,
            )


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


def _print_throttling(stats: GovernorStats) -> None:
    if stats.throttled_responses == 0 and stats.paused_seconds < 1:
        _stdout.print(
            "Server throttling: none observed (no HTTP 429/503 responses).", highlight=False
        )
        return
    _stdout.print(
        f"Server throttling: {stats.throttled_responses} HTTP 429/503 response(s), "
        f"circuit breaker opened {stats.breaker_trips} time(s); requests were paused "
        f"for {_format_remaining(stats.paused_seconds)} in total.",
        highlight=False,
    )


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
    cookie: Annotated[
        bool,
        typer.Option(
            "--cookie",
            help="Authenticate with a pasted browser 'connect.sid' cookie instead of "
            "username/password (prompted, hidden -- never a CLI argument). A per-run "
            "input: the session is never persisted, so this cookie is used for this "
            "invocation only. See also CDDPT_SESSION_COOKIE.",
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

    manual_cookie: SecretStr | None = None
    if cookie:
        raw_cookie = getpass.getpass("Paste your 'connect.sid' cookie value (input hidden): ")
        if not raw_cookie:
            raise AuthError("cddpt: no cookie value entered.")
        manual_cookie = SecretStr(raw_cookie)

    auth_manager = _build_auth_manager(settings, governor, cookie=manual_cookie)
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

    progress.start_run(
        len(plan.to_download),
        plan.remaining_known_bytes if plan.unknown_size_count == 0 else None,
    )
    governor.add_pause_listener(progress.pause_status)
    try:
        with progress:
            outcomes = downloader.run(plan, concurrency=concurrency, manifest_path=manifest)
    finally:
        governor.remove_pause_listener(progress.pause_status)

    _print_summary(outcomes)
    _print_throttling(governor.stats())
    _stderr.print(_common.license_reminder(known_collections, collection_ids))

    if any(o.status == DownloadStatus.failed for o in outcomes):
        raise typer.Exit(code=_common.EXIT_ERROR)


__all__ = ["LayoutChoice", "download_command"]
