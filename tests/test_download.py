"""Tests for :mod:`cddpt.download` (Milestone 5): token minting/exchange,
resumable transfer, concurrent orchestration, the manifest writer, and the
preflight/disk-space check.

Fully offline via the ``responses`` library (never vcrpy/real network for
anything auth-adjacent -- see docs/PLAN.md's SECRETS rules) against
synthetic ``search``/``download``/pre-signed-URL fixtures built in this
file. A tiny hand-rolled :class:`_FakeAuthProvider` (not
``KeycloakFormAuthProvider``) drives the auth-related scenarios so the
re-auth-sharing behaviour can be asserted precisely (call counts) without
depending on real Keycloak HTML fixtures.
"""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import responses
from responses import matchers
from shapely.geometry import Point

from cddpt.auth.base import AuthManager, AuthProvider, AuthSession, utcnow
from cddpt.catalog import CddCatalog
from cddpt.download import Downloader, DownloadPlan, PlannedDownload, ProgressCallback
from cddpt.errors import DownloadError, InsufficientDiskSpace
from cddpt.models import AssetRef, DownloadOutcome, DownloadStatus
from cddpt.naming import ByCollectionLayout, ByTileLayout, FlatLayout
from cddpt.ratelimit import RequestGovernor
from cddpt.settings import Settings

_POINT = Point(0, 0)

SITE_URL = "https://cdd.dgterritorio.gov.pt"
API_BASE = f"{SITE_URL}/dgt-be/v1"
SEARCH_URL = f"{API_BASE}/search"
TOKEN_URL = f"{API_BASE}/download/{{token}}"
LOGIN_URL = f"{SITE_URL}/auth/login"

PRESIGNED_1 = "https://stor-002.a.acnca.pt:9000/bucket/key1?X-Amz-Expires=3600&X-Amz-Signature=sig1"
PRESIGNED_2 = "https://stor-002.a.acnca.pt:9000/bucket/key2?X-Amz-Expires=3600&X-Amz-Signature=sig2"

_EXPIRED_PRESIGNED_BODY = '{"Code":"AccessDenied","Message":"Request has expired"}'
_SPENT_TOKEN_BODY = '{"status":403,"message":"Forbidden Access - Expired token or file not found"}'


def _settings(*, mint_batch_size: int | None = 1) -> Settings:
    """``mint_batch_size=1`` by default -- the old, one-token-at-a-time
    behaviour -- so every test in this file except the batch-minting ones
    (below) keeps asserting against single-id ``POST /search`` calls
    without needing to know batching exists at all. The batch-minting tests
    pass their own ``mint_batch_size`` explicitly."""

    return Settings(mint_batch_size=mint_batch_size)


def _fast_governor() -> RequestGovernor:
    """A governor that never actually throttles a test."""

    return RequestGovernor(requests_per_second=1000.0, burst=1000)


def _asset(
    *,
    item_id: str = "MDT-2m-111195-07-2024",
    collection_id: str = "MDT-2m",
    asset_key: str = "data",
    size_bytes: int | None = 1000,
    media_type: str = "image/tiff",
) -> AssetRef:
    return AssetRef(
        item_id=item_id,
        collection_id=collection_id,
        asset_key=asset_key,
        href="UNUSED-NEVER-READ",  # see download.py's module docstring, point 1
        size_bytes=size_bytes,
        media_type=media_type,
        tile_key=None,
        geometry=_POINT,
    )


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
    """A single-id, no-``limit`` ``POST /search`` -- what
    :meth:`~cddpt.download.Downloader._mint_href` sends directly (never
    through the batch pool): the spent-token re-mint retry, a batch
    response's missing-id fallback, and (with the default
    ``mint_batch_size=1`` -- see :func:`_settings`) every test in this file
    that isn't specifically about batching."""

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


def _batch_search_body(assets: list[AssetRef], tokens: list[str]) -> dict[str, object]:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": asset.item_id,
                "collection": asset.collection_id,
                "assets": {asset.asset_key: {"href": TOKEN_URL.format(token=token)}},
            }
            for asset, token in zip(assets, tokens, strict=True)
        ],
    }


def _register_batch_mint(
    assets: list[AssetRef], tokens: list[str], *, links: list[dict[str, object]] | None = None
) -> None:
    """The batch ``POST /search`` a worker's *first* mint attempt sends via
    :class:`~cddpt.download._MintPool` -- one collection, several ``ids``,
    and an explicit ``limit`` (see download.py's module docstring, point
    1). ``links`` optionally adds a STAC ``rel: "next"`` link to the
    response body, for the defensive-pagination test."""

    collection_id = assets[0].collection_id
    assert all(a.collection_id == collection_id for a in assets)
    body = _batch_search_body(assets, tokens)
    if links is not None:
        body["links"] = links
    responses.add(
        responses.POST,
        SEARCH_URL,
        json=body,
        status=200,
        match=[
            matchers.json_params_matcher(
                {
                    "collections": [collection_id],
                    "ids": [a.item_id for a in assets],
                    "limit": len(assets),
                }
            )
        ],
    )


def _register_exchange_redirect(token: str, location: str, *, status: int = 302) -> None:
    responses.add(
        responses.GET,
        TOKEN_URL.format(token=token),
        status=status,
        headers={"Location": location},
    )


def _register_exchange_forbidden(token: str, body: str = _SPENT_TOKEN_BODY) -> None:
    responses.add(
        responses.GET,
        TOKEN_URL.format(token=token),
        status=403,
        body=body,
        content_type="application/json",
    )


def _downloader(
    *,
    catalog: CddCatalog,
    auth: AuthManager,
    governor: RequestGovernor,
    settings: Settings | None = None,
    **kwargs: object,
) -> Downloader:
    return Downloader(
        catalog,
        auth,
        settings if settings is not None else _settings(),
        governor=governor,
        retry_sleep=lambda _seconds: None,
        **kwargs,  # type: ignore[arg-type]
    )


class _FakeAuthProvider:
    """A minimal :class:`~cddpt.auth.base.AuthProvider` handing out
    pre-built sessions in order (first ``authenticate()``, then every
    subsequent call -- whichever method -- the next one), tracking call
    counts so tests can assert exactly how many *real* re-authentications
    happened (as opposed to a cached/shared result)."""

    def __init__(self, sessions: list[AuthSession]) -> None:
        self._sessions = list(sessions)
        self._next = 0
        self.authenticate_calls = 0
        self.refresh_calls = 0

    def is_available(self) -> bool:
        return True

    def authenticate(self) -> AuthSession:
        self.authenticate_calls += 1
        return self._take()

    def refresh(self, session: AuthSession) -> AuthSession:
        self.refresh_calls += 1
        return self._take()

    def invalidate(self) -> None:
        pass

    def _take(self) -> AuthSession:
        index = min(self._next, len(self._sessions) - 1)
        self._next += 1
        return self._sessions[index]


def _auth_session(cookie_value: str) -> AuthSession:
    now = utcnow()
    return AuthSession(
        cookies={"connect.sid": cookie_value},
        obtained_at=now,
        expires_at=now + timedelta(minutes=30),
        source="form",
    )


def _auth_manager(provider: AuthProvider) -> AuthManager:
    return AuthManager(settings=_settings(), provider=provider)


def _simple_auth_manager(cookie_value: str = "sentinel-sid") -> AuthManager:
    return _auth_manager(_FakeAuthProvider([_auth_session(cookie_value)]))


# ---------------------------------------------------------------------------
# plan(): skip-existing, disk-space check, layout wiring
# ---------------------------------------------------------------------------


def test_plan_skips_already_complete_file_with_zero_network_calls(
    tmp_path: Path,
) -> None:
    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(catalog=catalog, auth=_simple_auth_manager(), governor=governor)

    asset = _asset(size_bytes=4)
    dest = ByCollectionLayout().dest_for(asset, tmp_path)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"data")

    plan = downloader.plan([asset], tmp_path, ByCollectionLayout())
    assert plan.to_download == ()
    assert len(plan.already_complete) == 1
    assert plan.already_present_bytes == 4

    outcomes = downloader.run(plan)
    assert len(outcomes) == 1
    assert outcomes[0].status == DownloadStatus.skipped
    assert outcomes[0].bytes_transferred == 4


def test_plan_raises_insufficient_disk_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(catalog=catalog, auth=_simple_auth_manager(), governor=governor)

    monkeypatch.setattr(
        "cddpt.download.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=0, used=0, free=100),
    )

    asset = _asset(size_bytes=10_000_000_000)
    with pytest.raises(InsufficientDiskSpace):
        downloader.plan([asset], tmp_path, ByCollectionLayout())


def test_plan_uses_given_layout(tmp_path: Path) -> None:
    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(catalog=catalog, auth=_simple_auth_manager(), governor=governor)

    asset = _asset(item_id="MDT-2m-111195-07-2024")
    plan = downloader.plan([asset], tmp_path, ByTileLayout())
    assert plan.to_download[0].dest == tmp_path / "111195" / "MDT-2m-111195-07-2024.tif"

    plan_flat = downloader.plan([asset], tmp_path, FlatLayout())
    assert plan_flat.to_download[0].dest == tmp_path / "MDT-2m-111195-07-2024.tif"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@responses.activate
def test_happy_path_download(tmp_path: Path) -> None:
    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(catalog=catalog, auth=_simple_auth_manager(), governor=governor)

    asset = _asset(size_bytes=1000)
    _register_mint(asset, "tok-1")
    _register_exchange_redirect("tok-1", PRESIGNED_1)
    responses.add(responses.GET, PRESIGNED_1, status=200, body=b"x" * 1000)

    plan = downloader.plan([asset], tmp_path, ByCollectionLayout())
    outcomes = downloader.run(plan)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.status == DownloadStatus.downloaded
    assert outcome.bytes_transferred == 1000
    assert outcome.error is None
    assert outcome.dest.read_bytes() == b"x" * 1000
    assert not outcome.dest.with_name(outcome.dest.name + ".part").exists()


# ---------------------------------------------------------------------------
# Resume via Range (206) / server ignores Range (200) restart / 416
# ---------------------------------------------------------------------------


@responses.activate
def test_resume_from_part_via_range_206(tmp_path: Path) -> None:
    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(catalog=catalog, auth=_simple_auth_manager(), governor=governor)

    asset = _asset(size_bytes=1000)
    dest = ByCollectionLayout().dest_for(asset, tmp_path)
    part = dest.with_name(dest.name + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(b"A" * 300)

    _register_mint(asset, "tok-1")
    _register_exchange_redirect("tok-1", PRESIGNED_1)
    responses.add(
        responses.GET,
        PRESIGNED_1,
        status=206,
        headers={"Content-Range": "bytes 300-999/1000", "Accept-Ranges": "bytes"},
        body=b"B" * 700,
        match=[matchers.header_matcher({"Range": "bytes=300-"})],
    )

    plan = downloader.plan([asset], tmp_path, ByCollectionLayout())
    outcomes = downloader.run(plan)

    assert outcomes[0].status == DownloadStatus.resumed
    assert outcomes[0].bytes_transferred == 1000
    assert outcomes[0].dest.read_bytes() == b"A" * 300 + b"B" * 700


@responses.activate
def test_mismatched_content_range_fails_and_discards_part(tmp_path: Path) -> None:
    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(catalog=catalog, auth=_simple_auth_manager(), governor=governor)

    asset = _asset(size_bytes=1000)
    dest = ByCollectionLayout().dest_for(asset, tmp_path)
    part = dest.with_name(dest.name + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(b"A" * 300)

    _register_mint(asset, "tok-1")
    _register_exchange_redirect("tok-1", PRESIGNED_1)
    responses.add(
        responses.GET,
        PRESIGNED_1,
        status=206,
        headers={"Content-Range": "bytes 500-999/1000"},
        body=b"C" * 500,
    )

    plan = downloader.plan([asset], tmp_path, ByCollectionLayout())
    outcomes = downloader.run(plan)

    assert outcomes[0].status == DownloadStatus.failed
    assert "Content-Range" in (outcomes[0].error or "")
    assert not part.exists()
    assert not dest.exists()


@responses.activate
def test_server_ignores_range_restarts_from_scratch(tmp_path: Path) -> None:
    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(catalog=catalog, auth=_simple_auth_manager(), governor=governor)

    asset = _asset(size_bytes=1000)
    dest = ByCollectionLayout().dest_for(asset, tmp_path)
    part = dest.with_name(dest.name + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(b"OLD-GARBAGE-DATA")  # not a prefix of the fresh content below

    _register_mint(asset, "tok-1")
    _register_exchange_redirect("tok-1", PRESIGNED_1)
    responses.add(responses.GET, PRESIGNED_1, status=200, body=b"F" * 1000)

    plan = downloader.plan([asset], tmp_path, ByCollectionLayout())
    outcomes = downloader.run(plan)

    # A pre-existing (if stale/garbage) .part file still counts as "resumed"
    # -- see DownloadStatus's docstring: it tracks whether a .part existed
    # beforehand, not whether the server actually honoured Range.
    assert outcomes[0].status == DownloadStatus.resumed
    assert outcomes[0].dest.read_bytes() == b"F" * 1000


@responses.activate
def test_416_with_matching_size_treated_as_complete(tmp_path: Path) -> None:
    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(catalog=catalog, auth=_simple_auth_manager(), governor=governor)

    asset = _asset(size_bytes=1000)
    dest = ByCollectionLayout().dest_for(asset, tmp_path)
    part = dest.with_name(dest.name + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(b"Z" * 1000)

    _register_mint(asset, "tok-1")
    _register_exchange_redirect("tok-1", PRESIGNED_1)
    responses.add(responses.GET, PRESIGNED_1, status=416)

    plan = downloader.plan([asset], tmp_path, ByCollectionLayout())
    # plan() itself can't know the .part is already complete (only dest.*
    # stats are consulted) -- this asset is legitimately still "to_download"
    # until the 416 short-circuit inside _download_asset resolves it.
    assert len(plan.to_download) == 1

    outcomes = downloader.run(plan)
    assert outcomes[0].status == DownloadStatus.resumed
    assert outcomes[0].dest.read_bytes() == b"Z" * 1000
    assert not part.exists()


# ---------------------------------------------------------------------------
# Mid-stream connection error -> resumed
# ---------------------------------------------------------------------------


@responses.activate
def test_mid_stream_connection_error_resumes(tmp_path: Path) -> None:
    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(catalog=catalog, auth=_simple_auth_manager(), governor=governor)

    asset = _asset(size_bytes=1000)
    dest = ByCollectionLayout().dest_for(asset, tmp_path)
    part = dest.with_name(dest.name + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(b"A" * 300)

    _register_mint(asset, "tok-1")
    _register_exchange_redirect("tok-1", PRESIGNED_1)
    # First transfer attempt (Range from the pre-existing .part): the
    # connection dies before any bytes arrive.
    responses.add(
        responses.GET,
        PRESIGNED_1,
        body=__import__("requests").exceptions.ConnectionError("connection reset"),
        match=[matchers.header_matcher({"Range": "bytes=300-"})],
    )
    # Retry: same offset (nothing new was written), succeeds this time.
    responses.add(
        responses.GET,
        PRESIGNED_1,
        status=206,
        headers={"Content-Range": "bytes 300-999/1000"},
        body=b"B" * 700,
        match=[matchers.header_matcher({"Range": "bytes=300-"})],
    )

    plan = downloader.plan([asset], tmp_path, ByCollectionLayout())
    outcomes = downloader.run(plan)

    assert outcomes[0].status == DownloadStatus.resumed
    assert outcomes[0].dest.read_bytes() == b"A" * 300 + b"B" * 700


# ---------------------------------------------------------------------------
# 403 spent token -> re-mint via ids search -> success
# ---------------------------------------------------------------------------


@responses.activate
def test_spent_token_remints_and_succeeds(tmp_path: Path) -> None:
    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(catalog=catalog, auth=_simple_auth_manager(), governor=governor)

    asset = _asset(size_bytes=1000)
    _register_mint(asset, "tok-1")
    _register_exchange_forbidden("tok-1")
    _register_mint(asset, "tok-2")
    _register_exchange_redirect("tok-2", PRESIGNED_1)
    responses.add(responses.GET, PRESIGNED_1, status=200, body=b"x" * 1000)

    plan = downloader.plan([asset], tmp_path, ByCollectionLayout())
    outcomes = downloader.run(plan)

    assert outcomes[0].status == DownloadStatus.downloaded
    assert outcomes[0].bytes_transferred == 1000


@responses.activate
def test_spent_token_twice_is_a_per_asset_failure(tmp_path: Path) -> None:
    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(catalog=catalog, auth=_simple_auth_manager(), governor=governor)

    asset = _asset(size_bytes=1000)
    _register_mint(asset, "tok-1")
    _register_exchange_forbidden("tok-1")
    _register_mint(asset, "tok-2")
    _register_exchange_forbidden("tok-2")

    plan = downloader.plan([asset], tmp_path, ByCollectionLayout())
    outcomes = downloader.run(plan)

    assert outcomes[0].status == DownloadStatus.failed
    assert "spent" in (outcomes[0].error or "") or "expired" in (outcomes[0].error or "")


# ---------------------------------------------------------------------------
# Pre-signed URL expired mid-transfer -> re-mint + exchange + Range-continue
# ---------------------------------------------------------------------------


@responses.activate
def test_presigned_url_expired_mid_transfer_remints_and_continues(tmp_path: Path) -> None:
    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(catalog=catalog, auth=_simple_auth_manager(), governor=governor)

    asset = _asset(size_bytes=1000)
    dest = ByCollectionLayout().dest_for(asset, tmp_path)
    part = dest.with_name(dest.name + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(b"A" * 400)  # already downloaded in an earlier attempt

    _register_mint(asset, "tok-1")
    _register_exchange_redirect("tok-1", PRESIGNED_1)
    responses.add(
        responses.GET,
        PRESIGNED_1,
        status=403,
        body=_EXPIRED_PRESIGNED_BODY,
        match=[matchers.header_matcher({"Range": "bytes=400-"})],
    )

    _register_mint(asset, "tok-2")
    _register_exchange_redirect("tok-2", PRESIGNED_2)
    responses.add(
        responses.GET,
        PRESIGNED_2,
        status=206,
        headers={"Content-Range": "bytes 400-999/1000"},
        body=b"B" * 600,
        match=[matchers.header_matcher({"Range": "bytes=400-"})],
    )

    plan = downloader.plan([asset], tmp_path, ByCollectionLayout())
    outcomes = downloader.run(plan)

    assert outcomes[0].status == DownloadStatus.resumed
    assert outcomes[0].dest.read_bytes() == b"A" * 400 + b"B" * 600

    # The second (post-re-mint) transfer call continued from the .part
    # file's existing size -- never restarted the whole file.
    presigned_2_calls = [c for c in responses.calls if c.request.url == PRESIGNED_2]
    assert len(presigned_2_calls) == 1
    assert presigned_2_calls[0].request.headers["Range"] == "bytes=400-"


# ---------------------------------------------------------------------------
# Size mismatch -> failed, .part kept, no final file
# ---------------------------------------------------------------------------


@responses.activate
def test_size_mismatch_fails_and_keeps_part_file(tmp_path: Path) -> None:
    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(catalog=catalog, auth=_simple_auth_manager(), governor=governor)

    asset = _asset(size_bytes=1000)
    _register_mint(asset, "tok-1")
    _register_exchange_redirect("tok-1", PRESIGNED_1)
    responses.add(responses.GET, PRESIGNED_1, status=200, body=b"x" * 500)  # short by 500 bytes

    plan = downloader.plan([asset], tmp_path, ByCollectionLayout())
    outcomes = downloader.run(plan)

    outcome = outcomes[0]
    assert outcome.status == DownloadStatus.failed
    assert "mismatch" in (outcome.error or "")
    assert not outcome.dest.exists()
    part = outcome.dest.with_name(outcome.dest.name + ".part")
    assert part.is_file()
    assert part.stat().st_size == 500


# ---------------------------------------------------------------------------
# 302-to-login mid-run -> a single re-auth shared across 2 concurrent assets
# ---------------------------------------------------------------------------


@responses.activate
def test_login_redirect_mid_run_triggers_single_shared_reauth(tmp_path: Path) -> None:
    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)

    provider = _FakeAuthProvider([_auth_session("old-sid"), _auth_session("new-sid")])
    auth = _auth_manager(provider)
    downloader = _downloader(catalog=catalog, auth=auth, governor=governor)

    asset_a = _asset(item_id="MDT-2m-A", size_bytes=1000)
    asset_b = _asset(item_id="MDT-2m-B", size_bytes=1000)

    for asset, token, presigned in (
        (asset_a, "tok-a", PRESIGNED_1),
        (asset_b, "tok-b", PRESIGNED_2),
    ):
        _register_mint(asset, token)
        # First exchange attempt (stale session): a login redirect.
        _register_exchange_redirect(token, LOGIN_URL)
        # Second exchange attempt (after the shared re-auth): success.
        _register_exchange_redirect(token, presigned)
        responses.add(responses.GET, presigned, status=200, body=b"x" * 1000)

    plan = downloader.plan([asset_a, asset_b], tmp_path, ByCollectionLayout())
    outcomes = downloader.run(plan, concurrency=2)

    assert {o.status for o in outcomes} == {DownloadStatus.downloaded}
    assert len(outcomes) == 2
    # Exactly one cold-start authenticate() and exactly one refresh() --
    # never one re-auth per thread/asset -- see AuthManager.on_unauthorized's
    # docstring and download.py's module docstring, point 2.
    assert provider.authenticate_calls == 1
    assert provider.refresh_calls == 1


# ---------------------------------------------------------------------------
# CDD cookies never sent to the S3 host
# ---------------------------------------------------------------------------


@responses.activate
def test_cdd_cookies_never_sent_to_s3_host(tmp_path: Path) -> None:
    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(
        catalog=catalog, auth=_simple_auth_manager("secret-sid-value"), governor=governor
    )

    asset = _asset(size_bytes=1000)
    _register_mint(asset, "tok-1")
    _register_exchange_redirect("tok-1", PRESIGNED_1)
    responses.add(responses.GET, PRESIGNED_1, status=200, body=b"x" * 1000)

    plan = downloader.plan([asset], tmp_path, ByCollectionLayout())
    downloader.run(plan)

    exchange_calls = [
        c for c in responses.calls if c.request.url.startswith(TOKEN_URL.format(token=""))
    ]
    transfer_calls = [c for c in responses.calls if c.request.url == PRESIGNED_1]
    assert exchange_calls and transfer_calls

    exchange_cookie = exchange_calls[0].request.headers.get("Cookie", "")
    assert "secret-sid-value" in exchange_cookie

    transfer_cookie = transfer_calls[0].request.headers.get("Cookie", "")
    assert "secret-sid-value" not in transfer_cookie
    assert "connect.sid" not in downloader._transfer_session.cookies


# ---------------------------------------------------------------------------
# Manifest contents: no secrets, correct fields
# ---------------------------------------------------------------------------


@responses.activate
def test_manifest_contains_no_secrets_and_expected_fields(tmp_path: Path) -> None:
    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(catalog=catalog, auth=_simple_auth_manager(), governor=governor)

    asset = _asset(item_id="MDT-2m-111195-07-2024", size_bytes=1000)
    _register_mint(asset, "tok-1")
    _register_exchange_redirect("tok-1", PRESIGNED_1)
    responses.add(responses.GET, PRESIGNED_1, status=200, body=b"x" * 1000)

    manifest_path = tmp_path / "manifest.json"
    plan = downloader.plan([asset], tmp_path, ByCollectionLayout())
    downloader.run(plan, manifest_path=manifest_path)

    raw = manifest_path.read_text(encoding="utf-8")
    assert "tok-1" not in raw
    assert "X-Amz-Signature" not in raw
    assert "sig1" not in raw

    payload = json.loads(raw)
    assert len(payload["outcomes"]) == 1
    entry = payload["outcomes"][0]
    assert entry["item_id"] == "MDT-2m-111195-07-2024"
    assert entry["collection_id"] == "MDT-2m"
    assert entry["status"] == "downloaded"
    assert entry["bytes_transferred"] == 1000
    assert entry["storage_filename"] == "MDT-2m-111195-07-2024.tif"
    assert entry["error"] is None


# ---------------------------------------------------------------------------
# concurrency= validation (hard cap, mirrors Settings' own policy)
# ---------------------------------------------------------------------------


def test_run_rejects_concurrency_above_hard_cap(tmp_path: Path) -> None:
    from cddpt.errors import ConfigError

    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(catalog=catalog, auth=_simple_auth_manager(), governor=governor)

    asset = _asset(size_bytes=4)
    # NOT already complete -- concurrency is only validated when there is
    # actually something to download; validation happens before the
    # ThreadPoolExecutor (and hence before any network call), so no mocks
    # are needed here for this to raise cleanly.
    plan = downloader.plan([asset], tmp_path, ByCollectionLayout())
    with pytest.raises(ConfigError):
        downloader.run(plan, concurrency=99)


# ---------------------------------------------------------------------------
# ProgressCallback is exercised (on_start/on_progress/on_done all fire)
# ---------------------------------------------------------------------------


class _RecordingProgress:
    def __init__(self) -> None:
        self.started: list[AssetRef] = []
        self.progressed: list[int] = []
        self.done: list[DownloadOutcome] = []

    def on_start(self, asset: AssetRef, total_bytes: int | None) -> None:
        self.started.append(asset)

    def on_progress(self, asset: AssetRef, nbytes: int) -> None:
        self.progressed.append(nbytes)

    def on_done(self, outcome: DownloadOutcome) -> None:
        self.done.append(outcome)


@responses.activate
def test_progress_callback_fires(tmp_path: Path) -> None:
    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    progress: ProgressCallback = _RecordingProgress()
    downloader = _downloader(
        catalog=catalog, auth=_simple_auth_manager(), governor=governor, progress=progress
    )

    asset = _asset(size_bytes=1000)
    _register_mint(asset, "tok-1")
    _register_exchange_redirect("tok-1", PRESIGNED_1)
    responses.add(responses.GET, PRESIGNED_1, status=200, body=b"x" * 1000)

    plan = downloader.plan([asset], tmp_path, ByCollectionLayout())
    downloader.run(plan)

    assert isinstance(progress, _RecordingProgress)
    assert len(progress.started) == 1
    assert sum(progress.progressed) == 1000
    assert len(progress.done) == 1
    assert progress.done[0].status == DownloadStatus.downloaded


# ---------------------------------------------------------------------------
# Clean cancellation: stops scheduling, keeps .part files, no exception
# ---------------------------------------------------------------------------


class _CancelAfterProgress:
    """A :class:`ProgressCallback` that sets a shared cancellation event
    once a byte threshold has been streamed -- mirrors how the live test
    plan (docs/PLAN.md's Milestone 5 deliverables) aborts a download
    mid-transfer via the progress callback."""

    def __init__(self, event: threading.Event, threshold: int) -> None:
        self._event = event
        self._threshold = threshold
        self._seen = 0

    def on_start(self, asset: AssetRef, total_bytes: int | None) -> None:
        pass

    def on_progress(self, asset: AssetRef, nbytes: int) -> None:
        self._seen += nbytes
        if self._seen >= self._threshold:
            self._event.set()

    def on_done(self, outcome: DownloadOutcome) -> None:
        pass


@responses.activate
def test_cancel_event_stops_mid_transfer_and_keeps_part_file(tmp_path: Path) -> None:
    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    cancel_event = threading.Event()
    progress = _CancelAfterProgress(cancel_event, threshold=300)
    downloader = Downloader(
        catalog,
        _simple_auth_manager(),
        _settings(),
        governor=governor,
        progress=progress,
        chunk_size=100,
        retry_sleep=lambda _seconds: None,
    )

    asset = _asset(size_bytes=1000)
    _register_mint(asset, "tok-1")
    _register_exchange_redirect("tok-1", PRESIGNED_1)
    responses.add(responses.GET, PRESIGNED_1, status=200, body=b"x" * 1000)

    plan = downloader.plan([asset], tmp_path, ByCollectionLayout())
    outcomes = downloader.run(plan, cancel_event=cancel_event)

    # A cancelled asset never appears in the returned outcomes at all (its
    # fate wasn't decided by this run) -- see Downloader.run()'s docstring.
    assert outcomes == []

    dest = plan.to_download[0].dest
    part = dest.with_name(dest.name + ".part")
    assert not dest.exists()
    assert part.is_file()
    assert 0 < part.stat().st_size < 1000


# ---------------------------------------------------------------------------
# Batch minting (mint_batch_size > 1): fewer /search POSTs, per-collection
# grouping, missing-id fallback, never-batched spent-token re-mint,
# thread-safety, and cancellation discarding pre-minted-but-unused hrefs.
# ---------------------------------------------------------------------------


def _presigned_for(item_id: str) -> str:
    return f"https://stor-002.a.acnca.pt:9000/bucket/{item_id}?X-Amz-Expires=3600"


@responses.activate
def test_batch_mint_used_for_multiple_same_collection_assets(tmp_path: Path) -> None:
    """10 assets, one collection, mint_batch_size=5 -> exactly 2 batch-mint
    POSTs (not 10)."""

    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(
        catalog=catalog,
        auth=_simple_auth_manager(),
        governor=governor,
        settings=_settings(mint_batch_size=5),
    )

    assets = [_asset(item_id=f"MDT-2m-E{i:02d}", size_bytes=10) for i in range(10)]
    tokens = [f"tok-e{i}" for i in range(10)]
    _register_batch_mint(assets[0:5], tokens[0:5])
    _register_batch_mint(assets[5:10], tokens[5:10])
    for asset, token in zip(assets, tokens, strict=True):
        presigned = _presigned_for(asset.item_id)
        _register_exchange_redirect(token, presigned)
        responses.add(responses.GET, presigned, status=200, body=b"x" * 10)

    plan = downloader.plan(assets, tmp_path, ByCollectionLayout())
    outcomes = downloader.run(plan, concurrency=1)

    assert len(outcomes) == 10
    assert all(o.status == DownloadStatus.downloaded for o in outcomes)
    search_posts = [c for c in responses.calls if c.request.url == SEARCH_URL]
    assert len(search_posts) == 2


@responses.activate
def test_batch_mint_groups_by_collection(tmp_path: Path) -> None:
    """Interleaved collections in plan order still produce one batch-mint
    POST per collection group -- a batch never mixes collections, since the
    /search filter is per-request."""

    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(
        catalog=catalog,
        auth=_simple_auth_manager(),
        governor=governor,
        settings=_settings(mint_batch_size=3),
    )

    a1 = _asset(item_id="MDT-2m-A1", collection_id="MDT-2m", size_bytes=10)
    b1 = _asset(item_id="LAZ-B1", collection_id="LAZ", size_bytes=10)
    a2 = _asset(item_id="MDT-2m-A2", collection_id="MDT-2m", size_bytes=10)
    b2 = _asset(item_id="LAZ-B2", collection_id="LAZ", size_bytes=10)
    a3 = _asset(item_id="MDT-2m-A3", collection_id="MDT-2m", size_bytes=10)
    b3 = _asset(item_id="LAZ-B3", collection_id="LAZ", size_bytes=10)
    assets = [a1, b1, a2, b2, a3, b3]
    tokens = ["tok-a1", "tok-b1", "tok-a2", "tok-b2", "tok-a3", "tok-b3"]

    _register_batch_mint([a1, a2, a3], ["tok-a1", "tok-a2", "tok-a3"])
    _register_batch_mint([b1, b2, b3], ["tok-b1", "tok-b2", "tok-b3"])
    for asset, token in zip(assets, tokens, strict=True):
        presigned = _presigned_for(asset.item_id)
        _register_exchange_redirect(token, presigned)
        responses.add(responses.GET, presigned, status=200, body=b"x" * 10)

    plan = downloader.plan(assets, tmp_path, ByCollectionLayout())
    outcomes = downloader.run(plan, concurrency=1)

    assert len(outcomes) == 6
    assert all(o.status == DownloadStatus.downloaded for o in outcomes)
    search_posts = [c for c in responses.calls if c.request.url == SEARCH_URL]
    assert len(search_posts) == 2


@responses.activate
def test_batch_mint_missing_id_falls_back_to_individual_mint(tmp_path: Path) -> None:
    """A batch response that omits one of the requested ids (a partial
    /search response) mints that one id individually instead of dropping it
    or failing the whole batch."""

    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(
        catalog=catalog,
        auth=_simple_auth_manager(),
        governor=governor,
        settings=_settings(mint_batch_size=3),
    )

    a1 = _asset(item_id="MDT-2m-F1", size_bytes=10)
    a2 = _asset(item_id="MDT-2m-F2", size_bytes=10)
    a3 = _asset(item_id="MDT-2m-F3", size_bytes=10)

    # The batch response only resolves a1 and a3 -- a2 is missing.
    responses.add(
        responses.POST,
        SEARCH_URL,
        json=_batch_search_body([a1, a3], ["tok-f1", "tok-f3"]),
        status=200,
        match=[
            matchers.json_params_matcher(
                {
                    "collections": ["MDT-2m"],
                    "ids": [a1.item_id, a2.item_id, a3.item_id],
                    "limit": 3,
                }
            )
        ],
    )
    # Fallback: a2 is minted individually (no "limit" -- see _mint_href).
    _register_mint(a2, "tok-f2")

    for asset, token in ((a1, "tok-f1"), (a2, "tok-f2"), (a3, "tok-f3")):
        presigned = _presigned_for(asset.item_id)
        _register_exchange_redirect(token, presigned)
        responses.add(responses.GET, presigned, status=200, body=b"x" * 10)

    plan = downloader.plan([a1, a2, a3], tmp_path, ByCollectionLayout())
    outcomes = downloader.run(plan, concurrency=1)

    assert len(outcomes) == 3
    assert all(o.status == DownloadStatus.downloaded for o in outcomes)


@responses.activate
def test_batch_minted_token_403_triggers_single_remint(tmp_path: Path) -> None:
    """A 403 (spent/expired) on a batch-minted token re-mints just that one
    asset directly -- never through the batch pool again."""

    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(
        catalog=catalog,
        auth=_simple_auth_manager(),
        governor=governor,
        settings=_settings(mint_batch_size=5),
    )

    asset = _asset(size_bytes=1000)
    _register_batch_mint([asset], ["tok-1"])
    _register_exchange_forbidden("tok-1")
    _register_mint(asset, "tok-2")  # single re-mint, no "limit"
    _register_exchange_redirect("tok-2", PRESIGNED_1)
    responses.add(responses.GET, PRESIGNED_1, status=200, body=b"x" * 1000)

    plan = downloader.plan([asset], tmp_path, ByCollectionLayout())
    outcomes = downloader.run(plan)

    assert outcomes[0].status == DownloadStatus.downloaded
    assert outcomes[0].bytes_transferred == 1000


@responses.activate
def test_batch_mint_concurrency_no_double_mint_or_lost_assets(tmp_path: Path) -> None:
    """2 worker threads, 9 same-collection assets, batch size 3: every asset
    is minted exactly once (never twice, never skipped) regardless of which
    thread happens to trigger each batch, and there are exactly 3 batch-mint
    POSTs (9 / 3)."""

    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(
        catalog=catalog,
        auth=_simple_auth_manager(),
        governor=governor,
        settings=_settings(mint_batch_size=3),
    )

    assets = [_asset(item_id=f"MDT-2m-G{i:02d}", size_bytes=10) for i in range(9)]
    asset_ids = {a.item_id for a in assets}
    requested_ids: list[str] = []
    lock = threading.Lock()

    def _mint_callback(request: object) -> tuple[int, dict[str, str], str]:
        payload = json.loads(getattr(request, "body"))  # noqa: B009
        ids = payload["ids"]
        assert payload["collections"] == ["MDT-2m"]
        assert payload["limit"] == len(ids)
        with lock:
            requested_ids.extend(ids)
        body = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "id": item_id,
                    "collection": "MDT-2m",
                    "assets": {"data": {"href": TOKEN_URL.format(token=f"tok-{item_id}")}},
                }
                for item_id in ids
            ],
        }
        return (200, {"Content-Type": "application/json"}, json.dumps(body))

    responses.add_callback(responses.POST, SEARCH_URL, callback=_mint_callback)

    for asset in assets:
        token = f"tok-{asset.item_id}"
        presigned = _presigned_for(asset.item_id)
        _register_exchange_redirect(token, presigned)
        responses.add(responses.GET, presigned, status=200, body=b"x" * 10)

    plan = downloader.plan(assets, tmp_path, ByCollectionLayout())
    outcomes = downloader.run(plan, concurrency=2)

    assert len(outcomes) == 9
    assert {o.asset.item_id for o in outcomes} == asset_ids
    assert all(o.status == DownloadStatus.downloaded for o in outcomes)
    # Every asset was requested exactly once across all batch-mint calls --
    # no double-mint, nothing lost.
    assert sorted(requested_ids) == sorted(asset_ids)
    search_posts = [c for c in responses.calls if c.request.url == SEARCH_URL]
    assert len(search_posts) == 3


@responses.activate
def test_batch_mint_transient_failure_only_fails_calling_asset(tmp_path: Path) -> None:
    """A batch-level mint failure (e.g. a transient HttpError after
    urllib3's own retries, or any other exception out of the mint call)
    must not fail every asset in that batch -- only the one asset whose
    worker actually triggered (and absorbed) it. The other batch members
    are simply un-claimed, never touching ``_errors``, so their own workers
    mint them fresh afterwards."""

    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    downloader = _downloader(
        catalog=catalog,
        auth=_simple_auth_manager(),
        governor=governor,
        settings=_settings(mint_batch_size=4),
    )

    assets = [_asset(item_id=f"MDT-2m-J{i:02d}", size_bytes=10) for i in range(4)]

    # A patched _mint_batch: fails outright the first time it's called (the
    # batch of 4 that the first worker's take() triggers), then behaves
    # normally -- see this module's docstring, "or a raised exception from
    # a patched _mint_batch".
    original_mint_batch = downloader._mint_batch
    call_count = 0

    def flaky_mint_batch(batch: list[AssetRef]) -> dict[object, object]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise DownloadError("cddpt: simulated transient batch-mint failure")
        return original_mint_batch(batch)

    downloader._mint_batch = flaky_mint_batch  # type: ignore[method-assign]

    def _mint_callback(request: object) -> tuple[int, dict[str, str], str]:
        payload = json.loads(getattr(request, "body"))  # noqa: B009
        ids = payload["ids"]
        assert payload["limit"] == len(ids)
        body = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "id": item_id,
                    "collection": "MDT-2m",
                    "assets": {"data": {"href": TOKEN_URL.format(token=f"tok-{item_id}")}},
                }
                for item_id in ids
            ],
        }
        return (200, {"Content-Type": "application/json"}, json.dumps(body))

    responses.add_callback(responses.POST, SEARCH_URL, callback=_mint_callback)

    for asset in assets:
        token = f"tok-{asset.item_id}"
        presigned = _presigned_for(asset.item_id)
        _register_exchange_redirect(token, presigned)
        responses.add(responses.GET, presigned, status=200, body=b"x" * 10)

    plan = downloader.plan(assets, tmp_path, ByCollectionLayout())
    outcomes = downloader.run(plan, concurrency=1)

    assert len(outcomes) == 4
    failed = [o for o in outcomes if o.status == DownloadStatus.failed]
    downloaded = [o for o in outcomes if o.status == DownloadStatus.downloaded]

    # Only the one asset that absorbed the batch-level failure ends up
    # failed -- the other three still download successfully, re-minted
    # individually on demand rather than being dragged down by someone
    # else's transient error.
    assert len(failed) == 1
    assert "simulated transient batch-mint failure" in (failed[0].error or "")
    assert len(downloaded) == 3
    assert {o.asset.item_id for o in downloaded} == {a.item_id for a in assets} - {
        failed[0].asset.item_id
    }
    # The mint call ran once (failing) for the batch, then again
    # successfully for whatever the other three assets needed -- retried,
    # never masked behind the first error.
    assert call_count >= 2


class _CancelAfterFirstDone:
    """A :class:`ProgressCallback` that cancels once ``item_id`` finishes --
    used to show that the *other* assets' hrefs, pre-minted in the very
    same batch call, are simply discarded (never exchanged) at
    cancellation."""

    def __init__(self, event: threading.Event, item_id: str) -> None:
        self._event = event
        self._item_id = item_id

    def on_start(self, asset: AssetRef, total_bytes: int | None) -> None:
        pass

    def on_progress(self, asset: AssetRef, nbytes: int) -> None:
        pass

    def on_done(self, outcome: DownloadOutcome) -> None:
        if outcome.asset.item_id == self._item_id:
            self._event.set()


@responses.activate
def test_batch_mint_cancellation_discards_preminted_unused_hrefs(tmp_path: Path) -> None:
    governor = _fast_governor()
    catalog = CddCatalog(settings=_settings(), governor=governor)
    cancel_event = threading.Event()

    assets = [_asset(item_id=f"MDT-2m-H{i:02d}", size_bytes=10) for i in range(4)]
    progress = _CancelAfterFirstDone(cancel_event, assets[0].item_id)
    downloader = Downloader(
        catalog,
        _simple_auth_manager(),
        _settings(mint_batch_size=4),
        governor=governor,
        progress=progress,
        retry_sleep=lambda _seconds: None,
    )

    tokens = [f"tok-h{i}" for i in range(4)]
    # A single batch mint covers all four -- batch_size=4 and only 4 assets.
    _register_batch_mint(assets, tokens)
    presigned_0 = _presigned_for(assets[0].item_id)
    _register_exchange_redirect(tokens[0], presigned_0)
    responses.add(responses.GET, presigned_0, status=200, body=b"x" * 10)
    # Deliberately no exchange/transfer mocks for tokens[1:] -- if the pool's
    # pre-minted-but-unused hrefs were ever exchanged, this test would fail
    # with a ConnectionError from an unregistered request instead.

    plan = downloader.plan(assets, tmp_path, ByCollectionLayout())
    outcomes = downloader.run(plan, concurrency=1, cancel_event=cancel_event)

    assert len(outcomes) == 1
    assert outcomes[0].asset.item_id == assets[0].item_id
    assert outcomes[0].status == DownloadStatus.downloaded
    search_posts = [c for c in responses.calls if c.request.url == SEARCH_URL]
    assert len(search_posts) == 1


# ---------------------------------------------------------------------------
# DownloadPlan / PlannedDownload are plain, importable dataclasses (smoke)
# ---------------------------------------------------------------------------


def test_download_plan_and_planned_download_are_exported() -> None:
    assert DownloadPlan is not None
    assert PlannedDownload is not None
