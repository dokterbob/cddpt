"""The download manager: token minting/exchange, resumable transfer, and
concurrent orchestration (Milestone 5).

Ground truth this module implements (docs/PLAN.md's M4 pre-work -- read that
section before touching this file)
-----------------------------------------------------------------------------
1. A search result's asset ``href`` (``.../v1/download/{token}``) is a
   **single-use** download token, minted per ``/search`` response. It is
   *never* read from an :class:`~cddpt.models.AssetRef` here -- this module
   always re-mints a fresh one right before use, via a tiny anonymous
   ``POST /search {"collections": [...], "ids": [item_id]}`` (cheap; no auth
   needed -- search itself is fully anonymous, see ``catalog.py``).
2. **Exchange**: ``GET /download/{token}`` with the CDD session cookie,
   ``allow_redirects=False``:
   - 302 to ``/auth/login`` -- the session is missing/expired
     (:func:`cddpt.auth.base.is_login_redirect`) -- call
     :meth:`~cddpt.auth.base.AuthManager.on_unauthorized` and retry the
     *same* token once (a token isn't invalidated by the session expiring);
     only re-mint if *that* retry then yields a 403.
   - 403 JSON ``{"status":403,"message":"..."}`` -- the token itself is
     spent/expired -- re-mint and retry the exchange once; a second 403 is a
     per-asset failure.
   - 302 to any other host -- success: a pre-signed S3 URL (MinIO,
     ``X-Amz-Expires=3600``).
3. **Transfer**: the pre-signed URL needs no cookies and is fetched with a
   *separate* governed session that never has CDD cookies applied (so they
   can never leak to the S3 host -- see :class:`Downloader.__init__`). It
   honours ``Range`` (206 + ``Content-Range``); a 200 despite a ``Range``
   request means the server ignored it, so the file is restarted from
   scratch; 416 means the ``.part`` may already be complete (checked against
   the asset's declared size); an expired pre-signed URL surfaces as an S3
   403 (``AccessDenied``/"Request has expired") mid-transfer, handled by
   re-minting + re-exchanging and continuing with ``Range`` from the
   ``.part`` file's current size -- the whole file is never restarted for
   this reason alone.
4. Every destination path is computed by a :class:`~cddpt.naming.Layout`
   from the :class:`~cddpt.models.AssetRef` alone, **before** any token is
   ever minted -- so :meth:`Downloader.plan` can skip an already-complete
   file with zero network calls (never spending a token on a file that's
   already there).
5. Final size is validated against ``asset.size_bytes`` when known (logged,
   never enforced, when absent -- e.g. orthophotos), then the ``.part`` file
   is atomically renamed onto the destination (:func:`os.replace`) -- a size
   mismatch is a failure that deliberately keeps the ``.part`` file (for
   inspection / a future resume attempt), never the final file.

Retry policy
------------
Two independent, bounded retry layers (both deliberately small -- see
docs/PLAN.md's "Backoff & rate-limiting policy": this module rides on top of
the same conservative, shared :class:`~cddpt.ratelimit.RequestGovernor` as
everything else in cddpt, so its own retries are about *correctness*
(resuming a token/session/URL that died), not about racing the rate limit):

- Minting + exchanging a usable pre-signed URL: at most two re-auth attempts
  and two re-mint attempts (see points 2-3 above) -- a handful of cheap
  metadata calls, not worth an exponential backoff of their own.
- The byte transfer itself: a bounded, exponential-backoff loop (via
  ``tenacity``, per docs/PLAN.md's explicit instruction) around one
  streaming attempt, retrying on a transient network error (resuming via
  ``Range`` from the ``.part`` file's current size) or an expired pre-signed
  URL (re-minted first). Any other exception (a real per-asset failure, or a
  deliberate cancellation) is never retried. A *stalled* stream counts as a
  transient network error: the transfer GET uses ``settings.stall_timeout``
  (default 30 s) as its socket read timeout, so a stream that delivers no
  bytes for that long is dropped and resumed via ``Range`` -- logged at
  WARNING -- instead of hanging silently for the full ``read_timeout``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import requests
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .auth.base import AuthManager, AuthSession, is_login_redirect
from .catalog import CddCatalog
from .errors import ConfigError, DownloadError, InsufficientDiskSpace
from .http import make_session
from .models import AssetRef, DownloadOutcome, DownloadStatus
from .naming import Layout
from .ratelimit import RequestGovernor
from .settings import Settings, validate_concurrency

logger = logging.getLogger(__name__)

#: Bytes read per HTTP chunk while streaming a transfer -- large enough to
#: keep per-chunk Python overhead low, small enough that progress callbacks
#: (and a cancellation check) still fire responsively on a slow connection.
_DEFAULT_CHUNK_SIZE = 1024 * 1024

#: Extra headroom (beyond the plan's own remaining-bytes total) required on
#: the destination filesystem before a run is allowed to start -- see
#: Downloader.plan()'s docstring.
_DEFAULT_DISK_MARGIN_BYTES = 1_000_000_000  # 1 GB

#: See module docstring, point 2: at most one re-auth retry (two attempts
#: total) of the *same* token before escalating to a re-mint.
_MAX_REAUTH_ATTEMPTS = 2
#: See module docstring, point 2: at most one re-mint retry (two attempts
#: total) before a per-asset failure.
_MAX_MINT_ATTEMPTS = 2
#: See module docstring, "Retry policy": bounded attempts for the byte
#: transfer itself (transient network errors / an expired pre-signed URL).
_MAX_TRANSFER_ATTEMPTS = 6

#: Substrings (checked case-insensitively) of an S3/MinIO error body that
#: indicate an expired pre-signed URL -- see module docstring, point 3.
_EXPIRED_PRESIGNED_MARKERS = ("accessdenied", "request has expired", "expired")


def _part_path(dest: Path) -> Path:
    """The ``.part`` path a destination is streamed to before being
    atomically renamed onto place."""

    return dest.with_name(dest.name + ".part")


def _existing_ancestor(path: Path) -> Path:
    """The nearest existing ancestor of ``path`` (itself, if it exists) --
    used so :func:`shutil.disk_usage` can be asked about a destination
    directory that hasn't been created yet."""

    candidate = path.resolve()
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            return candidate
        candidate = parent
    return candidate


class _Cancelled(Exception):
    """Raised internally to unwind a single asset's download when
    cancellation is requested -- never retried, never surfaced as a
    ``failed`` outcome (see :class:`Downloader.run`'s docstring)."""


class _SpentToken(Exception):
    """Raised internally when a download token was rejected as spent or
    expired (a 403 on the exchange) -- caught by :meth:`Downloader._get_presigned_url`,
    which re-mints and retries once."""


class _PresignedUrlExpired(Exception):
    """Raised internally when the pre-signed S3 URL itself has expired
    mid-transfer -- caught by :meth:`Downloader._download_asset`, which
    re-mints + re-exchanges for a fresh one and continues via ``Range``."""


#: Exceptions the transfer's outer retry loop treats as retryable (resume
#: via Range, or re-mint first for an expired pre-signed URL).
_TRANSIENT_TRANSFER_EXCEPTIONS: tuple[type[BaseException], ...] = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
    _PresignedUrlExpired,
)


class ProgressCallback(Protocol):
    """How a caller (the CLI, the QGIS plugin) observes download progress.

    Deliberately a plain Protocol with no default implementation pulled in
    here -- this library never imports ``tqdm``/``rich`` itself (per
    docs/PLAN.md: "Progress ... behind an injected callback Protocol ... QGIS
    swaps in ``QgsTask`` progress").
    """

    def on_start(self, asset: AssetRef, total_bytes: int | None) -> None:
        """Called once per asset before any network activity for it (even
        for a ``skipped`` outcome). ``total_bytes`` is ``asset.size_bytes``
        (``None`` if unknown)."""

    def on_progress(self, asset: AssetRef, nbytes: int) -> None:
        """Called with each newly-written chunk's size (not a running
        total) while ``asset`` is being streamed."""

    def on_done(self, outcome: DownloadOutcome) -> None:
        """Called exactly once per asset that reaches a final ``DownloadOutcome``
        (never for one left mid-flight by a cancellation)."""


class _NullProgress:
    """The default, no-op :class:`ProgressCallback`."""

    def on_start(self, asset: AssetRef, total_bytes: int | None) -> None:
        pass

    def on_progress(self, asset: AssetRef, nbytes: int) -> None:
        pass

    def on_done(self, outcome: DownloadOutcome) -> None:
        pass


@dataclass(frozen=True, slots=True)
class PlannedDownload:
    """One asset paired with its (deterministic, pre-exchange) destination."""

    asset: AssetRef
    dest: Path
    storage_filename: str


@dataclass(frozen=True, slots=True)
class DownloadPlan:
    """The result of :meth:`Downloader.plan`: what a subsequent
    :meth:`Downloader.run` would actually do, computed with zero network
    calls beyond whatever produced ``assets`` in the first place."""

    out_dir: Path
    to_download: tuple[PlannedDownload, ...]
    already_complete: tuple[PlannedDownload, ...]
    #: Sum of ``asset.size_bytes`` across every planned asset (download +
    #: already-complete) that has a known size.
    total_known_bytes: int
    #: Sum of ``asset.size_bytes`` across only ``to_download`` (i.e. what a
    #: subsequent ``run()`` would actually need to transfer).
    remaining_known_bytes: int
    #: Sum of ``asset.size_bytes`` across only ``already_complete``.
    already_present_bytes: int
    #: Assets (in either list) with no declared ``file:size`` -- never
    #: assumed to be 0 bytes, see ``catalog.py``.
    unknown_size_count: int
    #: Free space on the filesystem holding ``out_dir``, at plan time.
    free_disk_bytes: int


class _ManifestWriter:
    """Thread-safe, atomic JSON manifest writer.

    Rewrites the whole file (never appends) on every :meth:`record` call --
    simple and safe for the modest number of assets one run handles, and
    guarantees the manifest is always a single well-formed JSON document
    (even if the process is killed between writes, thanks to the
    ``.tmp`` + :func:`os.replace` swap). Never records a token, a pre-signed
    URL, or a cookie value -- see :func:`_outcome_to_manifest_dict`.
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._outcomes: list[DownloadOutcome] = []

    def record(self, outcome: DownloadOutcome) -> None:
        if self._path is None:
            return
        with self._lock:
            self._outcomes.append(outcome)
            payload = {"outcomes": [_outcome_to_manifest_dict(o) for o in self._outcomes]}
            tmp_path = self._path.with_name(self._path.name + ".tmp")
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(_dump_json(payload), encoding="utf-8")
            os.replace(tmp_path, self._path)


def _dump_json(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def _outcome_to_manifest_dict(outcome: DownloadOutcome) -> dict[str, object]:
    """An explicit field allowlist -- never ``asset.href`` (a single-use
    download token embedded in its own URL path) or anything else
    secret-shaped. See this module's docstring and ``models.py``'s
    ``DownloadOutcome`` docstring."""

    asset = outcome.asset
    return {
        "item_id": asset.item_id,
        "collection_id": asset.collection_id,
        "asset_key": asset.asset_key,
        "media_type": asset.media_type,
        "declared_size_bytes": asset.size_bytes,
        "dest": str(outcome.dest),
        "storage_filename": outcome.storage_filename,
        "status": outcome.status.value,
        "bytes_transferred": outcome.bytes_transferred,
        "error": outcome.error,
    }


def _looks_like_expired_presigned_url(response: requests.Response) -> bool:
    try:
        body = response.text
    except Exception:  # pragma: no cover -- defensive; a body we can't
        # decode is not itself evidence of expiry either way.
        return False
    lowered = body.lower()
    return any(marker in lowered for marker in _EXPIRED_PRESIGNED_MARKERS)


def _log_safe_url(url: str) -> str:
    """``scheme://host/path`` only, with any query string (a pre-signed S3
    URL's ``X-Amz-*`` parameters *are* its credential -- see this module's
    docstring and docs/PLAN.md's SECRETS rules) stripped before this is ever
    logged."""

    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _extract_forbidden_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return "403 Forbidden"
    if isinstance(payload, dict) and "message" in payload:
        return str(payload["message"])
    return "403 Forbidden"


class Downloader:
    """Plans and runs concurrent, resumable downloads of CDD assets.

    Parameters
    ----------
    catalog:
        Used only for its ``search_url`` -- token minting is a tiny,
        anonymous ``POST /search`` (see module docstring, point 1), not a
        full :class:`~cddpt.catalog.CddCatalog` search.
    auth:
        The shared :class:`~cddpt.auth.base.AuthManager` this run
        authenticates the CDD-host exchange requests with.
    settings:
        Defaults to a fresh :class:`~cddpt.settings.Settings` if omitted.
    governor:
        The shared :class:`~cddpt.ratelimit.RequestGovernor` for this run --
        pass the *same* instance used to build ``catalog``'s session and
        every other session in the run (docs/PLAN.md: "one shared
        RequestGovernor per run for ALL sessions"). Defaults to a fresh one
        built from ``settings`` if omitted (only sensible for a Downloader
        used entirely on its own).
    progress:
        Defaults to a no-op :class:`ProgressCallback`.

    Two separate governed sessions are built (both bound to the same
    governor, so they share one rate budget): ``self._cdd_session`` (used
    for minting + exchanging -- auth cookies are (re-)applied onto it right
    before every exchange, scoped to the CDD host only via
    :meth:`~cddpt.auth.base.AuthManager.apply`) and ``self._transfer_session``
    (used only for the pre-signed S3 URL -- *never* has ``auth.apply()``
    called on it, so a CDD session cookie can never reach the S3 host, even
    by accident).
    """

    def __init__(
        self,
        catalog: CddCatalog,
        auth: AuthManager,
        settings: Settings | None = None,
        *,
        governor: RequestGovernor | None = None,
        progress: ProgressCallback | None = None,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
        disk_margin_bytes: int = _DEFAULT_DISK_MARGIN_BYTES,
        retry_sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._catalog = catalog
        self._auth = auth
        self._settings = settings if settings is not None else Settings()
        self._governor = (
            governor if governor is not None else RequestGovernor.from_settings(self._settings)
        )
        self._progress: ProgressCallback = progress if progress is not None else _NullProgress()
        self._cdd_session = make_session(self._settings, governor=self._governor)
        self._transfer_session = make_session(self._settings, governor=self._governor)
        self._chunk_size = chunk_size
        self._disk_margin_bytes = disk_margin_bytes
        self._retry_sleep: Callable[[float], None] = (
            retry_sleep if retry_sleep is not None else time.sleep
        )

    # -- Planning -----------------------------------------------------------

    def plan(
        self,
        assets: Iterable[AssetRef],
        out_dir: Path,
        layout: Layout,
        *,
        overwrite: bool = False,
    ) -> DownloadPlan:
        """Compute what :meth:`run` would do for ``assets`` -- entirely
        offline (no network calls at all): every destination is
        :class:`~cddpt.naming.Layout`-computed from the
        :class:`~cddpt.models.AssetRef` alone, and "already complete" is
        decided purely from local filesystem stats.

        Raises :class:`~cddpt.errors.InsufficientDiskSpace` if the
        filesystem holding ``out_dir`` doesn't have enough free space for
        the assets that would actually need transferring (plus a safety
        margin) -- assets of unknown size are never counted against this
        check (never assumed to be 0 bytes), so it can under-count in their
        presence.
        """

        out_dir = Path(out_dir)
        to_download: list[PlannedDownload] = []
        already_complete: list[PlannedDownload] = []
        total_known_bytes = 0
        remaining_known_bytes = 0
        already_present_bytes = 0
        unknown_size_count = 0

        for asset in assets:
            dest = layout.dest_for(asset, out_dir)
            planned = PlannedDownload(asset=asset, dest=dest, storage_filename=dest.name)

            if asset.size_bytes is not None:
                total_known_bytes += asset.size_bytes
            else:
                unknown_size_count += 1

            is_complete = (
                not overwrite
                and dest.is_file()
                and (asset.size_bytes is None or dest.stat().st_size == asset.size_bytes)
            )
            if is_complete:
                already_complete.append(planned)
                if asset.size_bytes is not None:
                    already_present_bytes += asset.size_bytes
                continue

            to_download.append(planned)
            if asset.size_bytes is not None:
                remaining_known_bytes += asset.size_bytes

        free_disk_bytes = shutil.disk_usage(_existing_ancestor(out_dir)).free
        required = remaining_known_bytes + self._disk_margin_bytes
        if remaining_known_bytes > 0 and free_disk_bytes < required:
            raise InsufficientDiskSpace(
                f"cddpt: insufficient free disk space at {out_dir}: need at least "
                f"{required} bytes ({remaining_known_bytes} for the download(s) + "
                f"{self._disk_margin_bytes} margin), have {free_disk_bytes} free"
            )

        return DownloadPlan(
            out_dir=out_dir,
            to_download=tuple(to_download),
            already_complete=tuple(already_complete),
            total_known_bytes=total_known_bytes,
            remaining_known_bytes=remaining_known_bytes,
            already_present_bytes=already_present_bytes,
            unknown_size_count=unknown_size_count,
            free_disk_bytes=free_disk_bytes,
        )

    # -- Running --------------------------------------------------------------

    def run(
        self,
        plan: DownloadPlan,
        concurrency: int | None = None,
        *,
        manifest_path: Path | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[DownloadOutcome]:
        """Execute ``plan``: report every ``already_complete`` asset as a
        ``skipped`` outcome (no network), then download ``to_download`` with
        up to ``concurrency`` (default ``settings.concurrency``) worker
        threads.

        Cancellation (``cancel_event`` set by the caller from another
        thread, or a ``KeyboardInterrupt`` raised into this call) stops
        *scheduling* new work and lets in-flight transfers stop at their
        next chunk boundary -- a cancelled asset's ``.part`` file, if any,
        is left exactly as it was, ready for a future resume, and is never
        counted as ``failed`` (it just doesn't appear in the returned list
        at all, since its fate wasn't decided by this run). A
        ``KeyboardInterrupt`` is re-raised after every in-flight thread has
        wound down, so a caller can distinguish "interrupted" from
        "completed" while still being sure nothing was left in an
        inconsistent state.
        """

        event = cancel_event if cancel_event is not None else threading.Event()
        manifest = _ManifestWriter(manifest_path)
        outcomes: list[DownloadOutcome] = []

        for planned in plan.already_complete:
            self._progress.on_start(planned.asset, planned.asset.size_bytes)
            outcome = self._skip_outcome(planned)
            self._progress.on_done(outcome)
            manifest.record(outcome)
            outcomes.append(outcome)

        if not plan.to_download:
            return outcomes

        if concurrency is not None:
            # An explicit override bypasses Settings entirely -- apply the
            # exact same hard-cap/recommended-max policy
            # (docs/PLAN.md: "hard-capped at 4 ... never higher") rather
            # than letting a caller (e.g. `cddpt download --concurrency`)
            # silently exceed it.
            try:
                effective_concurrency = validate_concurrency(concurrency)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
        else:
            # settings.concurrency was already validated when Settings was
            # constructed.
            effective_concurrency = self._settings.concurrency

        try:
            with ThreadPoolExecutor(max_workers=effective_concurrency) as executor:
                futures = [
                    executor.submit(self._worker, planned, event, manifest)
                    for planned in plan.to_download
                ]
                for future in as_completed(futures):
                    result = future.result()
                    if result is not None:
                        outcomes.append(result)
        except BaseException:
            event.set()
            raise

        return outcomes

    def _skip_outcome(self, planned: PlannedDownload) -> DownloadOutcome:
        size = planned.dest.stat().st_size if planned.dest.is_file() else 0
        return DownloadOutcome(
            asset=planned.asset,
            dest=planned.dest,
            status=DownloadStatus.skipped,
            bytes_transferred=size,
            storage_filename=planned.storage_filename,
            error=None,
        )

    def _worker(
        self,
        planned: PlannedDownload,
        cancel_event: threading.Event,
        manifest: _ManifestWriter,
    ) -> DownloadOutcome | None:
        if cancel_event.is_set():
            return None

        asset = planned.asset
        part_path = _part_path(planned.dest)
        had_existing_part = part_path.is_file() and part_path.stat().st_size > 0
        self._progress.on_start(asset, asset.size_bytes)

        try:
            total = self._download_asset(planned, cancel_event)
            self._finalize(planned, total)
        except _Cancelled:
            return None
        except Exception as exc:
            # Any failure becomes a recorded, per-asset outcome rather than
            # aborting the whole run.
            bytes_so_far = part_path.stat().st_size if part_path.is_file() else 0
            outcome = DownloadOutcome(
                asset=asset,
                dest=planned.dest,
                status=DownloadStatus.failed,
                bytes_transferred=bytes_so_far,
                storage_filename=planned.storage_filename,
                error=str(exc),
            )
        else:
            status = DownloadStatus.resumed if had_existing_part else DownloadStatus.downloaded
            outcome = DownloadOutcome(
                asset=asset,
                dest=planned.dest,
                status=status,
                bytes_transferred=total,
                storage_filename=planned.storage_filename,
                error=None,
            )

        self._progress.on_done(outcome)
        manifest.record(outcome)
        return outcome

    def _finalize(self, planned: PlannedDownload, total_bytes: int) -> None:
        asset = planned.asset
        part_path = _part_path(planned.dest)
        if asset.size_bytes is not None and total_bytes != asset.size_bytes:
            raise DownloadError(
                f"cddpt: downloaded size mismatch for {asset.item_id}: expected "
                f"{asset.size_bytes} bytes, got {total_bytes} (kept {part_path.name} "
                "in place for a retry)"
            )
        if asset.size_bytes is None:
            logger.info(
                "cddpt: %s has no declared file:size; downloaded %d bytes without validation",
                asset.item_id,
                total_bytes,
            )
        os.replace(part_path, planned.dest)

    # -- Token minting + exchange --------------------------------------------

    def _mint_href(self, asset: AssetRef) -> str:
        """A fresh, single-use download-token href for ``asset`` -- never
        ``asset.href`` itself (see module docstring, point 1)."""

        response = self._cdd_session.post(
            self._catalog.search_url,
            json={"collections": [asset.collection_id], "ids": [asset.item_id]},
        )
        if response.status_code != 200:
            raise DownloadError(
                f"cddpt: minting a download token failed for {asset.item_id} "
                f"(HTTP {response.status_code})"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise DownloadError(
                f"cddpt: non-JSON /search response minting a download token for {asset.item_id}"
            ) from exc

        features = payload.get("features") if isinstance(payload, dict) else None
        if not features:
            raise DownloadError(
                f"cddpt: /search returned no item re-minting a download token for {asset.item_id}"
            )
        matching = next((f for f in features if f.get("id") == asset.item_id), features[0])
        assets = matching.get("assets") if isinstance(matching, dict) else None
        asset_payload = (assets or {}).get(asset.asset_key)
        if not isinstance(asset_payload, dict) or "href" not in asset_payload:
            raise DownloadError(
                f"cddpt: /search response is missing asset {asset.asset_key!r} for {asset.item_id}"
            )
        href = asset_payload["href"]
        if not isinstance(href, str) or not href:
            raise DownloadError(
                f"cddpt: malformed asset href re-minting a download token for {asset.item_id}"
            )
        return href

    def _exchange_with_reauth(self, href: str, asset: AssetRef) -> str:
        """Exchange ``href`` for a pre-signed URL, retrying once (with the
        *same* token) after a proactive re-auth if the session had expired.
        Raises :class:`_SpentToken` (propagated to the caller, which
        re-mints) on a 403."""

        for _ in range(_MAX_REAUTH_ATTEMPTS):
            failed_session: AuthSession = self._auth.current()
            self._auth.apply(self._cdd_session)
            response = self._cdd_session.get(href, allow_redirects=False)
            if is_login_redirect(response):
                self._auth.on_unauthorized(failed_session)
                continue
            return self._handle_exchange_response(response, asset)
        raise DownloadError(
            f"cddpt: the CDD session kept expiring while exchanging a download "
            f"token for {asset.item_id}"
        )

    def _handle_exchange_response(self, response: requests.Response, asset: AssetRef) -> str:
        if 300 <= response.status_code < 400:
            location = response.headers.get("Location")
            if not location:
                raise DownloadError(
                    f"cddpt: exchange for {asset.item_id} redirected with no Location header"
                )
            return location
        if response.status_code == 403:
            raise _SpentToken(_extract_forbidden_message(response))
        raise DownloadError(
            f"cddpt: unexpected HTTP {response.status_code} exchanging a download "
            f"token for {asset.item_id}"
        )

    def _get_presigned_url(self, asset: AssetRef) -> str:
        """Mint a fresh token and exchange it for a pre-signed URL, re-minting
        once more if the exchange reports the token as spent/expired (see
        module docstring, point 2)."""

        last_error: _SpentToken | None = None
        for _ in range(_MAX_MINT_ATTEMPTS):
            href = self._mint_href(asset)
            try:
                return self._exchange_with_reauth(href, asset)
            except _SpentToken as exc:
                last_error = exc
                continue
        detail = f" ({last_error})" if last_error is not None else ""
        raise DownloadError(
            f"cddpt: download token for {asset.item_id} was rejected as spent/expired "
            f"{_MAX_MINT_ATTEMPTS} time(s) in a row{detail}"
        )

    # -- Transfer -------------------------------------------------------------

    def _download_asset(self, planned: PlannedDownload, cancel_event: threading.Event) -> int:
        asset = planned.asset
        part_path = _part_path(planned.dest)
        part_path.parent.mkdir(parents=True, exist_ok=True)

        presigned_url = self._get_presigned_url(asset)

        def log_resume(state: RetryCallState) -> None:
            outcome = state.outcome
            error = outcome.exception() if outcome is not None else None
            if isinstance(error, _PresignedUrlExpired):
                reason = "pre-signed URL expired"
            elif isinstance(
                error, requests.exceptions.Timeout | requests.exceptions.ConnectionError
            ):
                reason = f"stalled or dropped connection ({type(error).__name__})"
            else:
                reason = type(error).__name__
            offset = part_path.stat().st_size if part_path.is_file() else 0
            wait = state.next_action.sleep if state.next_action is not None else 0.0
            logger.warning(
                "cddpt: transfer of %s interrupted (%s); resuming from byte %d in %.0fs "
                "(attempt %d of %d)",
                asset.item_id,
                reason,
                offset,
                wait,
                state.attempt_number + 1,
                _MAX_TRANSFER_ATTEMPTS,
            )

        retrying = Retrying(
            stop=stop_after_attempt(_MAX_TRANSFER_ATTEMPTS),
            wait=wait_exponential(multiplier=0.5, max=30),
            retry=retry_if_exception_type(_TRANSIENT_TRANSFER_EXCEPTIONS),
            sleep=self._retry_sleep,
            before_sleep=log_resume,
            reraise=True,
        )

        total = 0
        for attempt in retrying:
            with attempt:
                if cancel_event.is_set():
                    raise _Cancelled()
                start = part_path.stat().st_size if part_path.is_file() else 0
                try:
                    total = self._stream_once(presigned_url, asset, part_path, start, cancel_event)
                except _PresignedUrlExpired:
                    presigned_url = self._get_presigned_url(asset)
                    raise
        return total

    def _stream_once(
        self,
        url: str,
        asset: AssetRef,
        part_path: Path,
        start: int,
        cancel_event: threading.Event,
    ) -> int:
        headers: dict[str, str] = {}
        if start > 0:
            headers["Range"] = f"bytes={start}-"

        logger.info(
            "cddpt: streaming %s from %s (offset %d)",
            asset.item_id,
            _log_safe_url(url),
            start,
        )
        response = self._transfer_session.get(
            url,
            headers=headers,
            stream=True,
            # (connect, per-read) -- see the module docstring's "Retry policy":
            # a stalled stream fails after stall_timeout, then resumes.
            timeout=(self._settings.connect_timeout, self._settings.stall_timeout),
        )
        with response:
            if response.status_code == 416:
                declared = asset.size_bytes
                if declared is not None and start >= declared:
                    return start
                raise DownloadError(
                    f"cddpt: server returned 416 Range Not Satisfiable for {asset.item_id} "
                    f"but the partial file ({start} bytes) doesn't match the declared "
                    f"size ({declared!r})"
                )

            if response.status_code == 403:
                if _looks_like_expired_presigned_url(response):
                    raise _PresignedUrlExpired()
                raise DownloadError(f"cddpt: unexpected HTTP 403 downloading {asset.item_id}")

            if response.status_code == 206:
                content_range = response.headers.get("Content-Range", "")
                if content_range.startswith(f"bytes {start}-"):
                    mode, write_start = "ab", start
                elif content_range.startswith("bytes 0-"):
                    logger.warning(
                        "cddpt: server sent %s from offset 0 (requested %d); "
                        "restarting the file from scratch",
                        asset.item_id,
                        start,
                    )
                    mode, write_start = "wb", 0
                else:
                    # Any other offset can be neither appended nor written
                    # from zero without corrupting the file; discard the
                    # partial so the next attempt starts clean.
                    part_path.unlink(missing_ok=True)
                    raise DownloadError(
                        f"cddpt: unexpected Content-Range {content_range!r} for "
                        f"{asset.item_id} (requested offset {start}); discarded the "
                        "partial file"
                    )
            elif response.status_code == 200:
                if start > 0:
                    logger.info(
                        "cddpt: server ignored Range for %s; restarting the file from scratch",
                        asset.item_id,
                    )
                mode, write_start = "wb", 0
            else:
                raise DownloadError(
                    f"cddpt: unexpected HTTP {response.status_code} downloading {asset.item_id}"
                )

            total_written = write_start
            with open(part_path, mode) as fh:
                for chunk in response.iter_content(chunk_size=self._chunk_size):
                    if cancel_event.is_set():
                        raise _Cancelled()
                    if not chunk:
                        continue
                    fh.write(chunk)
                    total_written += len(chunk)
                    self._progress.on_progress(asset, len(chunk))
            return total_written


__all__ = [
    "DownloadPlan",
    "Downloader",
    "PlannedDownload",
    "ProgressCallback",
]
