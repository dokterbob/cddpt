"""Tests for ``cddpt download`` via :class:`typer.testing.CliRunner`
(Milestone 5).

The AOI/collection *selection* seam (``CddCatalog.collections``/
``iter_assets``, i.e. STAC/HTTP wire parsing) is monkeypatched at the class
level -- that machinery is already covered thoroughly by ``test_catalog.py``
and ``test_cli.py``'s ``search`` tests; this file's job is the `download`
command's OWN wiring: preflight/dry-run/confirmation/exit-code/keyring-
degradation behaviour. The actual token-mint/exchange/transfer network calls
made by :class:`cddpt.download.Downloader` (once auth resolves) are mocked
with ``responses``, exactly as in ``test_download.py``.
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pystac
import pytest
import responses
from responses import matchers
from rich.console import Console
from typer.testing import CliRunner

from cddpt.catalog import CddCatalog
from cddpt.cli import _common
from cddpt.cli import download as download_cli
from cddpt.cli.app import app
from cddpt.models import AssetRef, CollectionInfo, DownloadOutcome, DownloadStatus
from cddpt.ratelimit import GovernorPause, GovernorStats, PauseReason

FIXTURES = Path(__file__).parent / "fixtures" / "auth"

SITE_URL = "https://cdd.dgterritorio.gov.pt"
AUTH_URL = "https://auth.cdd.dgterritorio.gov.pt"
API_BASE = f"{SITE_URL}/dgt-be/v1"
SEARCH_URL = f"{API_BASE}/search"
TOKEN_URL = f"{API_BASE}/download/{{token}}"
LOGIN_URL_RE = re.compile(r"^https://cdd\.dgterritorio\.gov\.pt/auth/login.*")
AUTHORIZE_URL_RE = re.compile(
    r"^https://auth\.cdd\.dgterritorio\.gov\.pt/realms/dgterritorio/protocol/openid-connect/auth.*"
)
AUTHENTICATE_URL_RE = re.compile(
    r"^https://auth\.cdd\.dgterritorio\.gov\.pt/realms/dgterritorio/login-actions/authenticate.*"
)
CALLBACK_URL = f"{SITE_URL}/auth/callback"
DOWNLOADS_URL = f"{SITE_URL}/dgt-fe/downloads"

PRESIGNED_1 = "https://stor-002.a.acnca.pt:9000/bucket/key1?X-Amz-Expires=3600&X-Amz-Signature=sig1"

#: A rate policy fast enough to never actually sleep during a test.
_FAST_ENV = {"CDDPT_REQUESTS_PER_SECOND": "1000", "CDDPT_BURST": "1000"}

_LISBON_BBOX = "-9.15,38.70,-9.10,38.75"


@pytest.fixture(autouse=True)
def _reset_cli_state() -> None:
    _common.set_state(verbose=False, ca_bundle=None)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _collection_info(collection_id: str = "MDT-2m") -> CollectionInfo:
    collection = pystac.Collection(
        id=collection_id,
        description="test collection",
        extent=pystac.Extent(
            spatial=pystac.SpatialExtent([[-9.5, 36.9, -6.2, 42.2]]),
            temporal=pystac.TemporalExtent([[None, None]]),
        ),
        license="proprietary",
    )
    return CollectionInfo(
        id=collection_id,
        title=collection_id,
        description="test collection",
        license="proprietary",
        visibility=("show",),
        has_data_asset=None,
        collection=collection,
    )


def _asset(
    *,
    item_id: str = "MDT-2m-111195-07-2024",
    collection_id: str = "MDT-2m",
    size_bytes: int | None = 1000,
) -> AssetRef:
    from shapely.geometry import Point

    return AssetRef(
        item_id=item_id,
        collection_id=collection_id,
        asset_key="data",
        href="UNUSED-NEVER-READ",
        size_bytes=size_bytes,
        media_type="image/tiff",
        tile_key=None,
        geometry=Point(0, 0),
    )


def _patch_catalog(
    monkeypatch: pytest.MonkeyPatch,
    *,
    collections: list[CollectionInfo] | None = None,
    assets: list[AssetRef] | None = None,
) -> None:
    resolved_collections = collections if collections is not None else [_collection_info()]
    resolved_assets = assets if assets is not None else [_asset()]

    def fake_collections(self: CddCatalog) -> list[CollectionInfo]:
        return resolved_collections

    def fake_iter_assets(
        self: CddCatalog,
        aoi: object,
        collections: object,
        *,
        datetime: object = None,
        chunk_km2: object = None,
    ) -> object:
        yield from resolved_assets

    monkeypatch.setattr(CddCatalog, "collections", fake_collections)
    monkeypatch.setattr(CddCatalog, "iter_assets", fake_iter_assets)


def _search_body(asset: AssetRef, token: str) -> dict[str, object]:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": asset.item_id,
                "collection": asset.collection_id,
                "assets": {asset.asset_key: {"href": TOKEN_URL.format(token=token)}},
            }
        ],
    }


def _register_mint(asset: AssetRef, token: str) -> None:
    responses.add(
        responses.POST,
        SEARCH_URL,
        json=_search_body(asset, token),
        status=200,
        match=[
            matchers.json_params_matcher(
                {"collections": [asset.collection_id], "ids": [asset.item_id]}
            )
        ],
    )


def _register_exchange_redirect(token: str, location: str) -> None:
    responses.add(
        responses.GET,
        TOKEN_URL.format(token=token),
        status=302,
        headers={"Location": location},
    )


def _register_successful_login_chain(cookie_value: str = "sentinel-value") -> None:
    responses.add(
        responses.GET,
        LOGIN_URL_RE,
        status=302,
        headers={
            "Location": (
                f"{AUTH_URL}/realms/dgterritorio/protocol/openid-connect/auth"
                "?session_code=abc&execution=def&client_id=aai-oidc-dgt&tab_id=xyz&client_data=w"
            )
        },
    )
    responses.add(
        responses.GET,
        AUTHORIZE_URL_RE,
        status=200,
        body=(FIXTURES / "login_page.html").read_text(encoding="utf-8"),
        content_type="text/html; charset=utf-8",
    )
    responses.add(
        responses.POST, AUTHENTICATE_URL_RE, status=302, headers={"Location": CALLBACK_URL}
    )
    responses.add(
        responses.GET,
        CALLBACK_URL,
        status=302,
        headers={
            "Location": DOWNLOADS_URL,
            "Set-Cookie": (
                f"connect.sid=s%3A{cookie_value}.sig; Path=/; HttpOnly; "
                "Domain=cdd.dgterritorio.gov.pt"
            ),
        },
    )
    responses.add(responses.GET, DOWNLOADS_URL, status=200, body="<html>welcome</html>")


# ---------------------------------------------------------------------------
# Usage errors (exit 2) -- no network, no catalog patching needed
# ---------------------------------------------------------------------------


def test_download_missing_out_is_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(app, ["download", "--bbox", _LISBON_BBOX, "--collection", "LAZ"])
    assert result.exit_code == _common.EXIT_USAGE


def test_download_missing_collection_is_usage_error(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["download", "--out", str(tmp_path), "--bbox", _LISBON_BBOX])
    assert result.exit_code == _common.EXIT_USAGE


def test_download_no_aoi_is_usage_error(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["download", "--out", str(tmp_path), "--collection", "LAZ"])
    assert result.exit_code == _common.EXIT_USAGE


# ---------------------------------------------------------------------------
# Unknown collection -- a clean CddError, exit 1
# ---------------------------------------------------------------------------


def test_download_unknown_collection(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_catalog(monkeypatch, collections=[_collection_info("MDT-2m")])
    result = runner.invoke(
        app,
        [
            "download",
            "--out",
            str(tmp_path),
            "--bbox",
            _LISBON_BBOX,
            "--collection",
            "NOPE",
        ],
        env=_FAST_ENV,
    )
    assert result.exit_code == _common.EXIT_ERROR
    assert "unknown collection id" in result.stderr
    assert "NOPE" in result.stderr


# ---------------------------------------------------------------------------
# --dry-run: preflight printed, no auth, no download tokens spent
# ---------------------------------------------------------------------------


def test_download_dry_run_prints_preflight_and_touches_no_auth(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_keyring: object
) -> None:
    _patch_catalog(monkeypatch, assets=[_asset(size_bytes=1234)])

    # No `responses.activate` at all: if the command attempted so much as
    # one HTTP call (auth or otherwise), it would hit the real network and
    # this test would fail/hang/error -- that is the proof of "no auth
    # attempted".
    result = runner.invoke(
        app,
        [
            "download",
            "--out",
            str(tmp_path),
            "--bbox",
            _LISBON_BBOX,
            "--collection",
            "MDT-2m",
            "--dry-run",
        ],
        env=_FAST_ENV,
    )

    assert result.exit_code == _common.EXIT_OK, result.stderr
    assert "Download preflight" in result.stdout
    assert "Dry run" in result.stdout
    assert "proprietary" in result.stderr  # licence reminder, verbatim
    assert not (tmp_path / "MDT-2m").exists()


# ---------------------------------------------------------------------------
# Insufficient disk space -> exit 4
# ---------------------------------------------------------------------------


def test_download_insufficient_disk_exits_4(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_keyring: object
) -> None:
    from types import SimpleNamespace

    _patch_catalog(monkeypatch, assets=[_asset(size_bytes=10_000_000_000)])
    monkeypatch.setattr(
        "cddpt.download.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=0, used=0, free=100),
    )

    result = runner.invoke(
        app,
        [
            "download",
            "--out",
            str(tmp_path),
            "--bbox",
            _LISBON_BBOX,
            "--collection",
            "MDT-2m",
        ],
        env=_FAST_ENV,
    )

    assert result.exit_code == _common.EXIT_INSUFFICIENT_DISK, result.stderr


# ---------------------------------------------------------------------------
# Large-run confirmation: declining leaves nothing downloaded, exit 0
# ---------------------------------------------------------------------------


def test_download_declining_large_confirmation_aborts_cleanly(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_keyring: object
) -> None:
    from types import SimpleNamespace

    _patch_catalog(monkeypatch, assets=[_asset(size_bytes=6_000_000_000)])
    # Real free disk space on the test runner's tmp filesystem may be
    # smaller than this test's deliberately-large asset -- pin it well
    # above both the asset size and the confirmation threshold so this
    # test exercises the confirmation prompt, not the disk-space check.
    monkeypatch.setattr(
        "cddpt.download.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=0, used=0, free=1_000_000_000_000),
    )

    result = runner.invoke(
        app,
        [
            "download",
            "--out",
            str(tmp_path),
            "--bbox",
            _LISBON_BBOX,
            "--collection",
            "MDT-2m",
        ],
        input="n\n",
        env=_FAST_ENV,
    )

    assert result.exit_code == _common.EXIT_OK, result.stderr
    assert "Proceed with downloading" in result.stdout
    assert not (tmp_path / "MDT-2m").exists()


# ---------------------------------------------------------------------------
# --yes end-to-end flow, WITH the keyring unavailable but env creds present
# -- env credentials never touch the keyring at all, so a broken keyring
# backend must not matter.
# ---------------------------------------------------------------------------


@responses.activate
def test_download_yes_flow_with_broken_keyring_and_env_creds(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_keyring_backend: object,
) -> None:
    asset = _asset(item_id="MDT-2m-111195-07-2024", size_bytes=1000)
    _patch_catalog(monkeypatch, assets=[asset])

    _register_successful_login_chain()
    _register_mint(asset, "tok-1")
    _register_exchange_redirect("tok-1", PRESIGNED_1)
    responses.add(responses.GET, PRESIGNED_1, status=200, body=b"x" * 1000)

    env = {
        **_FAST_ENV,
        "CDDPT_USERNAME": "alice@example.test",
        "CDDPT_PASSWORD": "irrelevant",
    }

    result = runner.invoke(
        app,
        [
            "download",
            "--out",
            str(tmp_path),
            "--bbox",
            _LISBON_BBOX,
            "--collection",
            "MDT-2m",
            "--yes",
        ],
        env=env,
    )

    assert result.exit_code == _common.EXIT_OK, result.stderr
    assert "downloaded" in result.stdout.lower() or "Download summary" in result.stdout
    assert "Server throttling: none observed" in result.stdout

    dest = tmp_path / "MDT-2m" / "MDT-2m-111195-07-2024.tif"
    assert dest.is_file()
    assert dest.read_bytes() == b"x" * 1000


# ---------------------------------------------------------------------------
# --layout by-tile is actually honoured
# ---------------------------------------------------------------------------


@responses.activate
def test_download_layout_by_tile(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_keyring: object
) -> None:
    asset = _asset(item_id="MDT-2m-111195-07-2024", size_bytes=4)
    _patch_catalog(monkeypatch, assets=[asset])

    _register_successful_login_chain()
    _register_mint(asset, "tok-1")
    _register_exchange_redirect("tok-1", PRESIGNED_1)
    responses.add(responses.GET, PRESIGNED_1, status=200, body=b"data")

    env = {**_FAST_ENV, "CDDPT_USERNAME": "alice@example.test", "CDDPT_PASSWORD": "irrelevant"}
    result = runner.invoke(
        app,
        [
            "download",
            "--out",
            str(tmp_path),
            "--bbox",
            _LISBON_BBOX,
            "--collection",
            "MDT-2m",
            "--layout",
            "by-tile",
            "--yes",
        ],
        env=env,
    )

    assert result.exit_code == _common.EXIT_OK, result.stderr
    assert (tmp_path / "111195" / "MDT-2m-111195-07-2024.tif").is_file()


# ---------------------------------------------------------------------------
# Auth failure exits 3, before any download token is spent
# ---------------------------------------------------------------------------


@responses.activate
def test_download_auth_failure_exits_3(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_keyring: object
) -> None:
    asset = _asset(size_bytes=4)
    _patch_catalog(monkeypatch, assets=[asset])
    # No credentials at all, and the keyring (fake, empty -- never the real
    # OS keyring) has none stored either -- KeycloakFormAuthProvider's
    # authenticate() must raise AuthError.

    result = runner.invoke(
        app,
        [
            "download",
            "--out",
            str(tmp_path),
            "--bbox",
            _LISBON_BBOX,
            "--collection",
            "MDT-2m",
            "--yes",
        ],
        env=_FAST_ENV,
    )

    assert result.exit_code == _common.EXIT_AUTH_FAILURE, result.stderr


def test_download_broken_keyring_without_env_creds_exits_3(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_keyring_backend: object
) -> None:
    """No env credentials and a genuinely broken keyring backend -- the only
    other way to resolve credentials -- must be a clear AuthError (exit 3),
    not a silent degrade."""

    asset = _asset(size_bytes=4)
    _patch_catalog(monkeypatch, assets=[asset])

    result = runner.invoke(
        app,
        [
            "download",
            "--out",
            str(tmp_path),
            "--bbox",
            _LISBON_BBOX,
            "--collection",
            "MDT-2m",
            "--yes",
        ],
        env=_FAST_ENV,
    )

    assert result.exit_code == _common.EXIT_AUTH_FAILURE, result.stderr


# ---------------------------------------------------------------------------
# Live display: pause status line, finished bars removed, throttling summary
# ---------------------------------------------------------------------------


def _pause(reason: PauseReason, seconds: float, status: int | None = None) -> GovernorPause:
    return GovernorPause(
        pause_id=int(seconds * 1000) + list(PauseReason).index(reason),
        reason=reason,
        seconds=seconds,
        resume_at=datetime.now(timezone.utc) + timedelta(seconds=seconds),
        status=status,
    )


def _plain(renderable: object) -> str:
    console = Console(width=300, record=True, file=io.StringIO())
    console.print(renderable)
    return console.export_text()


def test_pause_status_shows_the_most_severe_pause_with_a_countdown() -> None:
    status = download_cli._PauseStatus()
    assert status.renderable() is None

    backoff = _pause(PauseReason.retry_backoff, 8.0, status=503)
    breaker = _pause(PauseReason.circuit_breaker, 600.0)
    status.on_pause(backoff)
    assert "Retrying after HTTP 503" in _plain(status.renderable())

    status.on_pause(breaker)
    text = _plain(status.renderable())
    assert "Circuit breaker open" in text
    assert "all requests paused until" in text
    assert re.search(r"in (9:5\d|10:00)", text), text

    status.on_resume(breaker)
    status.on_resume(backoff)
    assert status.renderable() is None


def test_pause_status_ignores_short_pauses() -> None:
    status = download_cli._PauseStatus()
    status.on_pause(_pause(PauseReason.retry_backoff, 0.5))
    assert status.renderable() is None


def test_pause_status_retry_after_message() -> None:
    status = download_cli._PauseStatus()
    status.on_pause(_pause(PauseReason.retry_after, 42.0))
    assert "Rate limited by the server (Retry-After)" in _plain(status.renderable())


def test_rich_progress_removes_finished_bars_and_counts_files(tmp_path: Path) -> None:
    console = Console(file=io.StringIO(), width=200)
    progress = download_cli._RichProgress(console=console)
    progress.start_run(2, 2000)
    first = _asset(item_id="A", size_bytes=1000)
    second = _asset(item_id="B", size_bytes=1000)

    progress.on_start(first, 1000)
    progress.on_start(second, 1000)
    progress.on_progress(first, 1000)
    progress.on_done(
        DownloadOutcome(
            asset=first,
            dest=tmp_path / "A.tif",
            status=DownloadStatus.downloaded,
            bytes_transferred=1000,
            storage_filename="A.tif",
            error=None,
        )
    )

    names = [task.fields["name"] for task in progress._progress.tasks]
    assert names == ["Total (1/2 files)", "B"]
    overall = progress._progress.tasks[0]
    assert overall.completed == 1000


def test_throttling_summary_reports_counts_and_paused_time(
    capsys: pytest.CaptureFixture[str],
) -> None:
    download_cli._print_throttling(
        GovernorStats(throttled_responses=7, breaker_trips=1, paused_seconds=612.0)
    )
    out = capsys.readouterr().out
    assert "7 HTTP 429/503 response(s)" in out
    assert "circuit breaker opened 1 time(s)" in out
    assert "10:12 in total" in out


def test_logging_and_live_display_share_one_console() -> None:
    """Log records must go through the console the live display runs on, or
    every refresh leaves a stale copy of the bars behind."""

    import logging

    from rich.logging import RichHandler

    _common.configure_logging(verbose=False)
    handlers = [h for h in logging.getLogger().handlers if isinstance(h, RichHandler)]
    assert handlers and handlers[0].console is _common.err_console
    assert download_cli._RichProgress()._progress.console is _common.err_console
    assert download_cli._stderr is _common.err_console
