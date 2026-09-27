"""Live Milestone 5 tests: real, small, polite downloads against the
authenticated CDD account.

Skipped unless ``CDDPT_USERNAME``/``CDDPT_PASSWORD`` are set (mirrors
``tests/test_auth_live.py``) and never run as part of the normal offline
suite (pytest's default ``addopts = "-m 'not network'"`` already deselects
``network``-marked tests too). MDT-2m only (~1 MB tiles) -- never LAZ
(~342 MB/tile). Auth is resolved purely from env vars, through an
:class:`~cddpt.auth.base.AuthManager` -- which caches its session in memory
only, by design -- so this must never depend on, or touch, the system
keyring (docs/PLAN.md's SECRETS note: the macOS keychain may be locked in
some environments).

Never logs/prints a token, cookie, or pre-signed URL query string -- see
docs/PLAN.md's SECRETS rules.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from cddpt.aoi import Aoi
from cddpt.auth.base import AuthManager
from cddpt.auth.form_provider import KeycloakFormAuthProvider
from cddpt.catalog import CddCatalog
from cddpt.download import Downloader
from cddpt.models import AssetRef, DownloadOutcome
from cddpt.naming import ByCollectionLayout
from cddpt.ratelimit import RequestGovernor
from cddpt.settings import Settings

pytestmark = pytest.mark.network

_USERNAME = os.environ.get("CDDPT_USERNAME")
_PASSWORD = os.environ.get("CDDPT_PASSWORD")

#: A tiny AOI over Lisbon -- deliberately small so MDT-2m (~1 MB/tile)
#: yields only a handful of tiles.
_TINY_BBOX = (-9.150, 38.700, -9.140, 38.710)

_MIN_ASSETS_NEEDED = 2

skip_reason = "CDDPT_USERNAME/CDDPT_PASSWORD not set -- skipping live download tests"
requires_live_creds = pytest.mark.skipif(not (_USERNAME and _PASSWORD), reason=skip_reason)


class _NullProgress:
    def on_start(self, asset: AssetRef, total_bytes: int | None) -> None:
        pass

    def on_progress(self, asset: AssetRef, nbytes: int) -> None:
        pass

    def on_done(self, outcome: DownloadOutcome) -> None:
        pass


def _settings() -> Settings:
    # Real credentials come from CDDPT_USERNAME/CDDPT_PASSWORD (env) --
    # Settings() picks those up automatically. Deliberately conservative
    # (default) rate limits -- this is a live run against DGT's real API.
    return Settings()


def _build_downloader(
    settings: Settings, governor: RequestGovernor, **kwargs: object
) -> tuple[Downloader, CddCatalog]:
    catalog = CddCatalog(settings=settings, governor=governor)
    # An in-memory-only session, resolved purely from env credentials --
    # never touches the system keyring (see this module's docstring and
    # docs/PLAN.md's SECRETS note).
    provider = KeycloakFormAuthProvider(settings=settings, governor=governor)
    auth = AuthManager(settings=settings, provider=provider)
    downloader = Downloader(catalog, auth, settings, governor=governor, **kwargs)  # type: ignore[arg-type]
    return downloader, catalog


def _find_small_assets(catalog: CddCatalog, count: int) -> list[AssetRef]:
    aoi = Aoi.from_bbox(*_TINY_BBOX)
    found: list[AssetRef] = []
    for asset in catalog.iter_assets(aoi, ["MDT-2m"]):
        found.append(asset)
        if len(found) >= count:
            break
    return found


@requires_live_creds
def test_live_download_two_small_tiles(tmp_path: Path) -> None:
    """(a) Download 2 real MDT-2m tiles to a tmp dir; sizes match
    ``file:size``."""

    settings = _settings()
    governor = RequestGovernor.from_settings(settings)
    downloader, catalog = _build_downloader(settings, governor, progress=_NullProgress())

    assets = _find_small_assets(catalog, _MIN_ASSETS_NEEDED)
    if len(assets) < _MIN_ASSETS_NEEDED:
        pytest.skip(f"fewer than {_MIN_ASSETS_NEEDED} MDT-2m assets found in the tiny live AOI")

    plan = downloader.plan(assets, tmp_path, ByCollectionLayout())
    outcomes = downloader.run(plan)

    assert len(outcomes) == len(assets)
    for outcome in outcomes:
        assert outcome.status.value == "downloaded"
        assert outcome.error is None
        assert outcome.dest.is_file()
        if outcome.asset.size_bytes is not None:
            assert outcome.dest.stat().st_size == outcome.asset.size_bytes


@requires_live_creds
def test_live_resume_via_range_after_abort(tmp_path: Path) -> None:
    """(b) Abort a tile's download after ~200 KB (via the progress
    callback), then re-run: assert a Range/206 continuation was used and the
    file completes correctly."""

    settings = _settings()
    governor = RequestGovernor.from_settings(settings)

    cancel_event = threading.Event()

    class _AbortAfterThreshold:
        def __init__(self) -> None:
            self.seen = 0

        def on_start(self, asset: AssetRef, total_bytes: int | None) -> None:
            pass

        def on_progress(self, asset: AssetRef, nbytes: int) -> None:
            self.seen += nbytes
            if self.seen >= 200_000:
                cancel_event.set()

        def on_done(self, outcome: DownloadOutcome) -> None:
            pass

    abort_progress = _AbortAfterThreshold()
    downloader, catalog = _build_downloader(settings, governor, progress=abort_progress)

    assets = _find_small_assets(catalog, 1)
    if not assets:
        pytest.skip("no MDT-2m assets found in the tiny live AOI")
    asset = assets[0]
    if asset.size_bytes is None or asset.size_bytes < 300_000:
        pytest.skip("selected asset has no declared size, or is too small to abort mid-transfer")

    plan = downloader.plan([asset], tmp_path, ByCollectionLayout())
    outcomes = downloader.run(plan, cancel_event=cancel_event)
    assert outcomes == []  # cancelled -- never a decided outcome

    dest = plan.to_download[0].dest
    part = dest.with_name(dest.name + ".part")
    assert part.is_file()
    partial_size = part.stat().st_size
    assert 0 < partial_size < asset.size_bytes

    # Re-run: spy on the transfer session to confirm a 206/Range
    # continuation was used, without ever logging a pre-signed URL's query
    # string (only scheme://host/path -- see docs/PLAN.md's SECRETS rules).
    resume_calls: list[tuple[str | None, int]] = []
    original_get = downloader._transfer_session.get

    def _spying_get(url: str, **kwargs: object) -> object:
        response = original_get(url, **kwargs)  # type: ignore[misc]
        headers = kwargs.get("headers") or {}
        range_header = headers.get("Range") if isinstance(headers, dict) else None  # type: ignore[union-attr]
        resume_calls.append((range_header, response.status_code))  # type: ignore[attr-defined]
        return response

    downloader._transfer_session.get = _spying_get  # type: ignore[method-assign]

    plan2 = downloader.plan([asset], tmp_path, ByCollectionLayout())
    outcomes2 = downloader.run(plan2)

    assert len(outcomes2) == 1
    assert outcomes2[0].status.value in ("resumed", "downloaded")
    assert outcomes2[0].dest.is_file()
    assert outcomes2[0].dest.stat().st_size == asset.size_bytes

    assert resume_calls, "expected at least one transfer request on resume"
    range_header, status_code = resume_calls[0]
    assert range_header == f"bytes={partial_size}-"
    assert status_code == 206


@requires_live_creds
def test_live_rerun_is_a_zero_exchange_noop(tmp_path: Path) -> None:
    """(c) Re-running a fully-downloaded plan is a no-op: zero exchange
    requests (the destination file already matches ``file:size``)."""

    settings = _settings()
    governor = RequestGovernor.from_settings(settings)
    downloader, catalog = _build_downloader(settings, governor, progress=_NullProgress())

    assets = _find_small_assets(catalog, 1)
    if not assets:
        pytest.skip("no MDT-2m assets found in the tiny live AOI")

    plan = downloader.plan(assets, tmp_path, ByCollectionLayout())
    outcomes = downloader.run(plan)
    assert all(o.status.value == "downloaded" for o in outcomes)

    exchange_calls = 0
    original_get = downloader._cdd_session.get
    original_post = downloader._cdd_session.post

    def _counting_get(*args: object, **kwargs: object) -> object:
        nonlocal exchange_calls
        exchange_calls += 1
        return original_get(*args, **kwargs)  # type: ignore[misc]

    def _counting_post(*args: object, **kwargs: object) -> object:
        nonlocal exchange_calls
        exchange_calls += 1
        return original_post(*args, **kwargs)  # type: ignore[misc]

    downloader._cdd_session.get = _counting_get  # type: ignore[method-assign]
    downloader._cdd_session.post = _counting_post  # type: ignore[method-assign]

    plan2 = downloader.plan(assets, tmp_path, ByCollectionLayout())
    assert plan2.to_download == ()
    assert len(plan2.already_complete) == len(assets)

    outcomes2 = downloader.run(plan2)
    assert all(o.status.value == "skipped" for o in outcomes2)
    assert exchange_calls == 0
