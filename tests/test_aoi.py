"""Tests for cddpt.aoi.Aoi: construction, reprojection, and chunking.

No network access anywhere in this file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pyproj import Geod
from shapely.geometry import Point, box

from cddpt.aoi import Aoi
from cddpt.errors import ConfigError

FIXTURES = Path(__file__).parent / "fixtures"
#: A small Lisbon-area bbox, matching the AOI used for cddpt's own live
#: catalog cassette recordings (see tests/test_catalog.py).
_LISBON_BBOX = (-9.15, 38.70, -9.10, 38.75)


def _geod_area_km2(aoi: Aoi) -> float:
    """An independent (geodesic, CRS-agnostic) area estimate, used only to
    sanity-check Aoi.area_km2()'s EPSG:3763-based result."""

    area, _ = Geod(ellps="WGS84").geometry_area_perimeter(aoi.geometry)
    return abs(area) / 1_000_000.0


# ---------------------------------------------------------------------------
# from_bbox
# ---------------------------------------------------------------------------


def test_from_bbox_bounds_and_area() -> None:
    aoi = Aoi.from_bbox(*_LISBON_BBOX)
    assert aoi.bounds == pytest.approx(_LISBON_BBOX)
    assert aoi.area_km2() == pytest.approx(_geod_area_km2(aoi), rel=1e-2)


def test_from_bbox_invalid_raises() -> None:
    with pytest.raises(ConfigError):
        Aoi.from_bbox(0, 0, 0, 1)  # west == east
    with pytest.raises(ConfigError):
        Aoi.from_bbox(0, 1, 1, 0)  # south > north


# ---------------------------------------------------------------------------
# from_wkt
# ---------------------------------------------------------------------------


def test_from_wkt_default_crs_is_wgs84() -> None:
    aoi = Aoi.from_wkt("POLYGON((-9.15 38.70, -9.10 38.70, -9.10 38.75, -9.15 38.75, -9.15 38.70))")
    assert aoi.bounds == pytest.approx(_LISBON_BBOX)


def test_from_wkt_reprojects_from_explicit_crs() -> None:
    # A 1km x 1km square in EPSG:3763 matching tile (113, 194) -- see
    # tiles.py/test_tiles.py.
    wkt = (
        "POLYGON((-87000 -107000, -86000 -107000, -86000 -106000, -87000 -106000, -87000 -107000))"
    )
    aoi = Aoi.from_wkt(wkt, crs="EPSG:3763")
    assert aoi.area_km2() == pytest.approx(1.0, rel=1e-3)
    # Lands in the right part of Portugal (Lisbon area).
    minx, miny, maxx, maxy = aoi.bounds
    assert -9.2 < minx < maxx < -9.0
    assert 38.6 < miny < maxy < 38.8


def test_from_wkt_invalid_wkt_raises() -> None:
    with pytest.raises(ConfigError):
        Aoi.from_wkt("NOT WKT AT ALL")


# ---------------------------------------------------------------------------
# from_geojson
# ---------------------------------------------------------------------------


def test_from_geojson_bare_geometry_dict() -> None:
    geom = {
        "type": "Polygon",
        "coordinates": [
            [[-9.15, 38.70], [-9.10, 38.70], [-9.10, 38.75], [-9.15, 38.75], [-9.15, 38.70]]
        ],
    }
    aoi = Aoi.from_geojson(geom)
    assert aoi.bounds == pytest.approx(_LISBON_BBOX)


def test_from_geojson_feature() -> None:
    feature = {
        "type": "Feature",
        "properties": {},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [[-9.15, 38.70], [-9.10, 38.70], [-9.10, 38.75], [-9.15, 38.75], [-9.15, 38.70]]
            ],
        },
    }
    aoi = Aoi.from_geojson(feature)
    assert aoi.bounds == pytest.approx(_LISBON_BBOX)


def test_from_geojson_json_text() -> None:
    geom = {
        "type": "Polygon",
        "coordinates": [
            [[-9.15, 38.70], [-9.10, 38.70], [-9.10, 38.75], [-9.15, 38.75], [-9.15, 38.70]]
        ],
    }
    aoi = Aoi.from_geojson(json.dumps(geom))
    assert aoi.bounds == pytest.approx(_LISBON_BBOX)


def test_from_geojson_no_type_raises() -> None:
    with pytest.raises(ConfigError):
        Aoi.from_geojson({"coordinates": []})


def test_from_geojson_unparseable_text_raises() -> None:
    with pytest.raises(ConfigError):
        Aoi.from_geojson("{not json")


def test_from_geojson_legacy_crs_and_union_from_file() -> None:
    """The full scenario the spec calls out: a FeatureCollection with a
    legacy top-level 'crs' member (EPSG:3763), two features that must be
    unioned into one geometry, reprojected to roughly the right place in
    Portugal."""

    fixture = FIXTURES / "two_tiles_epsg3763.geojson"
    aoi = Aoi.from_geojson(fixture)

    # The two fixture tiles are adjacent 1km x 1km squares -> union area
    # is exactly their combined 2 km^2, not 1 km^2 (i.e. really unioned)
    # and not 0 (i.e. not empty/miscombined).
    assert aoi.area_km2() == pytest.approx(2.0, rel=1e-3)
    assert aoi.geometry.geom_type == "Polygon"  # touching squares merge cleanly

    # Roughly the right place in Portugal (Lisbon area, per the fixture's
    # real EPSG:3763 coordinates -- see tiles.py/test_tiles.py for the same
    # tile IDs' provenance).
    minx, miny, maxx, maxy = aoi.bounds
    assert -9.2 < minx < maxx < -9.0
    assert 38.6 < miny < maxy < 38.8


def test_from_geojson_legacy_crs_as_json_text() -> None:
    fixture = FIXTURES / "two_tiles_epsg3763.geojson"
    aoi = Aoi.from_geojson(fixture.read_text(encoding="utf-8"))
    assert aoi.area_km2() == pytest.approx(2.0, rel=1e-3)


def test_from_geojson_unrecognised_crs_raises() -> None:
    data = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
        "crs": {"type": "name", "properties": {"name": "not a real crs at all"}},
    }
    with pytest.raises(ConfigError):
        Aoi.from_geojson(data)


# ---------------------------------------------------------------------------
# from_file (optional cddpt[files] extra)
# ---------------------------------------------------------------------------


def test_from_file_geopackage_roundtrip(tmp_path: Path) -> None:
    geopandas = pytest.importorskip("geopandas")

    gdf = geopandas.GeoDataFrame(
        {"id": [1]},
        geometry=[box(-87000, -107000, -86000, -106000)],  # tile (113, 194), see tiles.py
        crs="EPSG:3763",
    )
    path = tmp_path / "aoi.gpkg"
    gdf.to_file(path, driver="GPKG")

    aoi = Aoi.from_file(path)
    assert aoi.area_km2() == pytest.approx(1.0, rel=1e-3)


def test_from_file_missing_extra_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "geopandas", None)
    with pytest.raises(ConfigError, match="cddpt\\[files\\]"):
        Aoi.from_file("does-not-matter.gpkg")


# ---------------------------------------------------------------------------
# chunks()
# ---------------------------------------------------------------------------


def test_chunks_single_chunk_when_under_threshold() -> None:
    aoi = Aoi.from_bbox(*_LISBON_BBOX)
    chunks = aoi.chunks(1000.0)
    assert chunks == [aoi]


def test_chunks_splits_and_conserves_area() -> None:
    aoi = Aoi.from_bbox(*_LISBON_BBOX)
    chunks = aoi.chunks(6.0)
    assert len(chunks) > 1
    assert sum(c.area_km2() for c in chunks) == pytest.approx(aoi.area_km2(), rel=1e-9)
    for chunk in chunks:
        # Each chunk is a real (non-empty) piece.
        assert not chunk.geometry.is_empty
        assert chunk.area_km2() > 0


def test_chunks_drops_empty_grid_cells_for_l_shaped_aoi() -> None:
    """An L-shaped AOI's bounding-box grid has one corner cell that doesn't
    intersect the AOI at all -- it must not appear as a spurious empty/tiny
    chunk, and area must still be conserved exactly (grid-aligned, so no
    floating-point ambiguity)."""

    wkt = "POLYGON((0 0, 2000 0, 2000 1000, 1000 1000, 1000 2000, 0 2000, 0 0))"
    aoi = Aoi.from_wkt(wkt, crs="EPSG:3763")
    assert aoi.area_km2() == pytest.approx(3.0, rel=1e-6)

    chunks = aoi.chunks(1.0)  # 1 km^2 cells over a 2km x 2km bounding box
    assert len(chunks) == 3  # not 4: the missing corner cell is dropped
    for chunk in chunks:
        assert chunk.area_km2() == pytest.approx(1.0, rel=1e-6)
    assert sum(c.area_km2() for c in chunks) == pytest.approx(3.0, rel=1e-6)


def test_chunks_two_adjacent_chunks_straddle_a_shared_tile() -> None:
    """The exact AOI/threshold cddpt's own live cassette test uses (see
    tests/test_catalog.py) -- verified here purely geometrically, offline:
    both chunks intersect the same real tile (113, 194)."""

    from cddpt.tiles import decode_tile_key, tile_extent_3763

    aoi = Aoi.from_bbox(-9.13209740894674, 38.70334513247749, -9.122957895932906, 38.70747642257685)
    chunks = aoi.chunks(0.25)
    assert len(chunks) == 2

    key = decode_tile_key("MDT-2m-113194-07-2024")
    assert key is not None
    tile_box = box(*tile_extent_3763(key))

    from cddpt.aoi import _PORTUGAL_TM06, _WGS84, _reproject

    for chunk in chunks:
        chunk_metric = _reproject(chunk.geometry, _WGS84, _PORTUGAL_TM06)
        assert chunk_metric.intersects(tile_box)


def test_chunks_invalid_max_km2_raises() -> None:
    aoi = Aoi.from_bbox(*_LISBON_BBOX)
    with pytest.raises(ConfigError):
        aoi.chunks(0.0)
    with pytest.raises(ConfigError):
        aoi.chunks(-5.0)


def test_to_geojson_roundtrips_via_from_geojson() -> None:
    aoi = Aoi.from_bbox(*_LISBON_BBOX)
    geojson = aoi.to_geojson()
    assert geojson["type"] == "Polygon"
    roundtripped = Aoi.from_geojson(geojson)
    assert roundtripped.bounds == pytest.approx(aoi.bounds)


def test_area_km2_outside_mainland_uses_equal_area_fallback() -> None:
    """An AOI far outside EPSG:3763's area of use (e.g. mid-Atlantic, near
    the Azores) must still produce a sane area via the equal-area fallback,
    not silently misbehave."""

    # A small ~1 degree box near the Azores (well outside continental
    # Portugal's EPSG:3763 area of use).
    aoi = Aoi.from_bbox(-28.5, 37.5, -28.4, 37.6)
    assert aoi.area_km2() == pytest.approx(_geod_area_km2(aoi), rel=1e-2)


def test_bounds_and_geometry_are_wgs84_point_sanity() -> None:
    # A degenerate (zero-area) AOI is not something cddpt is expected to
    # chunk meaningfully, but area_km2()/bounds must not raise.
    aoi = Aoi(geometry=Point(-9.13, 38.70))
    assert aoi.area_km2() == 0.0
    assert aoi.chunks(1.0) == [aoi]
