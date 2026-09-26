"""cddpt.catalog tests.

Two kinds of test live here:

- Cassette-backed tests (this file's majority): replay real, previously
  recorded responses from ``tests/cassettes/`` via the ``cdd_vcr`` fixture
  (see ``conftest.py``), with ``record_mode="none"`` by default -- these run
  fully offline, every time, and are *not* marked ``network``.
- Pure unit tests of envelope-parsing/sanitizing logic against small
  synthetic payloads (no network, no cassette needed at all).

To re-record the cassettes (only ever needed if the live API's shape
changes): ``CDDPT_VCR_RECORD=once uv run pytest tests/test_catalog.py -m
network --no-header`` -- but note none of these are marked ``network``
themselves; re-recording is a manual/local operation (see this repo's
milestone notes), not something CI ever does.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pystac
import pytest

from cddpt.aoi import Aoi
from cddpt.catalog import CddCatalog, _parse_envelope, _sanitize_item_bbox
from cddpt.errors import ApiError, ConfigError
from cddpt.settings import Settings

#: A fast, effectively non-throttling rate policy for tests -- correctness
#: of the governor itself is covered by test_ratelimit.py/test_http.py.
_FAST_SETTINGS = Settings(_env_file=None, requests_per_second=1000.0, burst=1000)


def _catalog() -> CddCatalog:
    return CddCatalog(settings=_FAST_SETTINGS)


# ---------------------------------------------------------------------------
# Cassette-backed tests
# ---------------------------------------------------------------------------


def test_collections_envelope_shape(cassette: None) -> None:
    """GET /collections: envelope unwrap, and where visibility actually
    lives (summaries.visibility -- see catalog.py's docstring)."""

    catalog = _catalog()
    collections = catalog.collections()

    assert len(collections) >= 20
    by_id = {c.id: c for c in collections}
    assert "MDT-2m" in by_id
    assert "LAZ" in by_id

    mdt2m = by_id["MDT-2m"]
    assert mdt2m.visibility == ("show",)
    # Verbatim, never pystac's migrated "other" -- see catalog.py docstring.
    assert mdt2m.license == "proprietary"
    assert isinstance(mdt2m.collection, pystac.Collection)

    # The Azores collections are visibility=hide; mainland ones are show.
    acores = [c for c in collections if c.id.startswith("ACORES-")]
    assert acores
    assert all(c.visibility == ("hide",) for c in acores)

    downloadable = catalog.downloadable_collection_ids()
    assert "MDT-2m" in downloadable
    assert "LAZ" in downloadable
    assert all(not cid.startswith("ACORES-") for cid in downloadable)


def test_collection_detail_envelope_shape(cassette: None) -> None:
    catalog = _catalog()
    info = catalog.collection("MDT-2m")
    assert info.id == "MDT-2m"
    assert info.license == "proprietary"
    assert info.visibility == ("show",)


def test_search_raster_collection_bbox_is_sanitized(cassette: None) -> None:
    """MDT-2m items from this recorded batch have the corrupted
    geotransform bbox on the wire -- iter_items() must never expose it."""

    catalog = _catalog()
    aoi = Aoi.from_bbox(-9.15, 38.70, -9.10, 38.75)

    items = list(catalog.iter_items(aoi, ["MDT-2m"], chunk_km2=1000.0, page_size=5))
    assert items
    for item in items:
        assert item.bbox is not None
        minx, miny, maxx, maxy = item.bbox
        # A real geotransform-corrupted bbox has huge EPSG:3763-scale
        # magnitudes (tens of thousands); a sanitized WGS84 bbox for a
        # Portuguese tile does not.
        assert -180 <= minx <= maxx <= 180
        assert -90 <= miny <= maxy <= 90
        # The bbox must match the item's own geometry bounds exactly.
        from shapely.geometry import shape

        assert (minx, miny, maxx, maxy) == pytest.approx(shape(item.geometry).bounds)
        # The original (corrupted) bbox is preserved for inspection.
        assert "cddpt:raw_bbox" in item.properties


def test_search_laz_collection_asset_role(cassette: None) -> None:
    catalog = _catalog()
    aoi = Aoi.from_bbox(-9.15, 38.70, -9.10, 38.75)

    assets = list(catalog.iter_assets(aoi, ["LAZ"], chunk_km2=1000.0, page_size=5))
    assert assets
    for asset in assets:
        assert asset.asset_key == "data"
        assert asset.collection_id == "LAZ"
        assert asset.size_bytes is not None and asset.size_bytes > 0
        assert asset.tile_key is not None
        assert asset.tile_key.prefix == "LO"


def test_search_multi_page_pagination_dedups_and_completes(cassette: None) -> None:
    """A small limit (5) on a small AOI forces several /search pages;
    pystac-client's real 'next' link + POST body pagination must fetch them
    all with no duplicate item IDs."""

    catalog = _catalog()
    aoi = Aoi.from_bbox(-9.15, 38.70, -9.10, 38.75)

    items = list(catalog.iter_items(aoi, ["MDT-2m"], chunk_km2=1000.0, page_size=5))
    ids = [item.id for item in items]

    assert len(ids) > 5, "expected more than one page's worth of items"
    assert len(ids) == len(set(ids)), "duplicate item IDs across pages"


def test_search_two_chunks_dedups_straddling_tile(cdd_vcr: object) -> None:
    """A small AOI, chunked into exactly 2 pieces that both intersect the
    *same* single MDT-2m tile, must yield that item exactly once."""

    catalog = _catalog()
    # See docs/PLAN.md-adjacent test-authoring notes: this bbox sits
    # entirely inside tile (113, 194) and chunk_km2=0.25 forces the AOI's
    # ~0.36 km^2 area to split into exactly 2 chunks straddling that tile.
    aoi = Aoi.from_bbox(-9.13209740894674, 38.70334513247749, -9.122957895932906, 38.70747642257685)
    chunks = aoi.chunks(0.25)
    assert len(chunks) == 2, "test AOI/threshold must produce exactly 2 chunks"

    with cdd_vcr.use_cassette("test_search_two_chunks_dedups_straddling_tile.yaml") as cas:  # type: ignore[attr-defined]
        items = list(catalog.iter_items(aoi, ["MDT-2m"], chunk_km2=0.25, page_size=10))
        assert [item.id for item in items] == ["MDT-2m-113194-07-2024"]
        # Two distinct chunk searches actually reached the wire (proving
        # this is a real dedup across chunk boundaries, not a fluke of a
        # single search) -- len(cas.requests) works both when recording and
        # when replaying, unlike play_count (replay-only).
        assert len(cas.requests) == 2


# ---------------------------------------------------------------------------
# Pure unit tests (no network, no cassette)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, json_body: object) -> None:
        self.status_code = status_code
        self._json_body = json_body

    def json(self) -> object:
        return self._json_body


def test_parse_envelope_ok() -> None:
    response = _FakeResponse(200, {"status": 200, "message": "OK", "data": {"foo": "bar"}})
    assert _parse_envelope(response, "http://example/") == {"foo": "bar"}  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "response",
    [
        _FakeResponse(404, {}),
        _FakeResponse(200, {"message": "OK", "data": {}}),  # missing "status"
        _FakeResponse(200, {"status": 500, "message": "err", "data": {}}),
        _FakeResponse(200, "not a dict"),
        _FakeResponse(200, {"status": 200, "message": "OK"}),  # missing "data"
    ],
)
def test_parse_envelope_bad_envelope_raises_api_error(response: _FakeResponse) -> None:
    with pytest.raises(ApiError):
        _parse_envelope(response, "http://example/")  # type: ignore[arg-type]


def test_sanitize_item_bbox_overwrites_corrupted_bbox() -> None:
    item = pystac.Item(
        id="MDT-2m-113194-07-2024",
        geometry={
            "type": "Polygon",
            "coordinates": [
                [
                    [-9.1333284, 38.7091897],
                    [-9.1332029, 38.7001829],
                    [-9.1217092, 38.7002807],
                    [-9.1218332, 38.7092875],
                    [-9.1333284, 38.7091897],
                ]
            ],
        },
        bbox=[-87000, 2, 0, -106000, 0, -2],  # corrupted geotransform
        datetime=datetime(2024, 7, 16, tzinfo=timezone.utc),
        properties={},
    )
    _sanitize_item_bbox(item)
    assert item.bbox is not None
    minx, miny, maxx, maxy = item.bbox
    assert -180 <= minx <= maxx <= 180
    assert -90 <= miny <= maxy <= 90
    assert item.properties["cddpt:raw_bbox"] == [-87000, 2, 0, -106000, 0, -2]


def test_iter_items_requires_explicit_collections() -> None:
    catalog = _catalog()
    aoi = Aoi.from_bbox(-9.15, 38.70, -9.10, 38.75)
    with pytest.raises(ConfigError):
        list(catalog.iter_items(aoi, []))


def test_estimate_requires_explicit_collections() -> None:
    catalog = _catalog()
    aoi = Aoi.from_bbox(-9.15, 38.70, -9.10, 38.75)
    with pytest.raises(ConfigError):
        catalog.estimate(aoi, [])
