"""Tests for cddpt.tiles: the reverse-engineered tile ID scheme.

The ``REAL_TILE_PAIRS`` table below is not synthetic: every ``item_id`` and
its expected ``(tile_x, tile_y)`` origin was taken from real, live
``POST /search`` responses (2026-09) across several collections (``MDT-2m``,
``LAZ``, ``MDT-50cm``, ``MDS-2m``, ``MDS-50cm``) and two regions (Lisbon and
Porto). Two independent lines of evidence back the origin convention this
module implements (``originY`` = the tile's *top*, maximum-Y edge):

1. For raster items whose corrupted ``bbox`` *is* the literal GDAL
   geotransform (e.g. ``MDT-2m-113194-07-2024``, ``bbox ==
   [-87000, 2, 0, -106000, 0, -2]``), ``bbox[0]``/``bbox[3]`` match
   ``originX``/``originY`` computed from the tile ID *exactly*.
2. For LAZ items (whose ``bbox`` is always a genuine WGS84 bbox, never
   corrupted -- e.g. ``LO-113194-07-2024``, ``LO-160467-07-2025``), that
   bbox reprojected to EPSG:3763 lands within ~10m of
   ``[originX, originY - 1000, originX + 1000, originY]`` (the small
   discrepancy is real LiDAR tile buffer/overlap, not measurement error).

Several entries below intentionally share a tile across different
collections/years (e.g. ``MDT-2m-113194-07-2024`` and
``LO-113194-07-2024``; ``LO-160467-07-2025``, ``MDT-50cm-160467-07-2025``,
``MDS-50cm-162467-07-2025``, ...) -- this is deliberate: it demonstrates the
tile grid is shared across collections, not per-collection.
"""

from __future__ import annotations

import pytest

from cddpt.tiles import TileKey, decode_tile_key, tile_extent_3763

#: (item_id, expected TileKey fields) -- see module docstring for provenance.
REAL_TILE_PAIRS: list[tuple[str, TileKey]] = [
    # --- MDT-2m, Lisbon-area bbox, 2024 batch (corrupted geotransform bbox
    # confirms origin directly: bbox[0]=originX, bbox[3]=originY). ---
    ("MDT-2m-113194-07-2024", TileKey("MDT-2m-113194-07-2024", "MDT-2m", 113, 194, 7, 2024)),
    ("MDT-2m-113193-06-2024", TileKey("MDT-2m-113193-06-2024", "MDT-2m", 113, 193, 6, 2024)),
    ("MDT-2m-114199-07-2024", TileKey("MDT-2m-114199-07-2024", "MDT-2m", 114, 199, 7, 2024)),
    ("MDT-2m-114198-07-2024", TileKey("MDT-2m-114198-07-2024", "MDT-2m", 114, 198, 7, 2024)),
    ("MDT-2m-114197-07-2024", TileKey("MDT-2m-114197-07-2024", "MDT-2m", 114, 197, 7, 2024)),
    # --- LAZ, same Lisbon-area bbox: real (never-corrupted) WGS84 bboxes,
    # independently reprojected to EPSG:3763 and cross-checked against the
    # MDT-2m geotransform pairs above (same tiles). ---
    ("LO-113194-07-2024", TileKey("LO-113194-07-2024", "LO", 113, 194, 7, 2024)),
    ("LO-113193-06-2024", TileKey("LO-113193-06-2024", "LO", 113, 193, 6, 2024)),
    ("LO-114199-07-2024", TileKey("LO-114199-07-2024", "LO", 114, 199, 7, 2024)),
    ("LO-114198-07-2024", TileKey("LO-114198-07-2024", "LO", 114, 198, 7, 2024)),
    ("LO-114197-07-2024", TileKey("LO-114197-07-2024", "LO", 114, 197, 7, 2024)),
    # --- LAZ, Porto-area bbox, 2025 batch: independently reprojected and
    # verified (see module docstring point 2). ---
    ("LO-160467-07-2025", TileKey("LO-160467-07-2025", "LO", 160, 467, 7, 2025)),
    ("LO-159466-07-2025", TileKey("LO-159466-07-2025", "LO", 159, 466, 7, 2025)),
    ("LO-160466-07-2025", TileKey("LO-160466-07-2025", "LO", 160, 466, 7, 2025)),
    # --- Other Porto-area collections, same tile grid, 2025 batch (bbox NOT
    # corrupted for this batch -- see catalog.py's docstring -- but the tile
    # ID -> origin mapping is identical regardless). ---
    ("MDT-50cm-160467-07-2025", TileKey("MDT-50cm-160467-07-2025", "MDT-50cm", 160, 467, 7, 2025)),
    ("MDS-2m-162467-07-2025", TileKey("MDS-2m-162467-07-2025", "MDS-2m", 162, 467, 7, 2025)),
    ("MDS-2m-161467-07-2025", TileKey("MDS-2m-161467-07-2025", "MDS-2m", 161, 467, 7, 2025)),
    ("MDS-2m-162463-07-2025", TileKey("MDS-2m-162463-07-2025", "MDS-2m", 162, 463, 7, 2025)),
    ("MDT-2m-162467-07-2025", TileKey("MDT-2m-162467-07-2025", "MDT-2m", 162, 467, 7, 2025)),
    ("MDS-50cm-162467-07-2025", TileKey("MDS-50cm-162467-07-2025", "MDS-50cm", 162, 467, 7, 2025)),
]

assert len(REAL_TILE_PAIRS) >= 12, "spec requires >= 12 real (item id -> expected origin) pairs"


@pytest.mark.parametrize("item_id,expected", REAL_TILE_PAIRS, ids=[p[0] for p in REAL_TILE_PAIRS])
def test_decode_real_tile_ids(item_id: str, expected: TileKey) -> None:
    assert decode_tile_key(item_id) == expected


@pytest.mark.parametrize(
    "item_id,expected_origin",
    [
        ("MDT-2m-113194-07-2024", (-87000.0, -106000.0)),
        ("MDT-2m-113193-06-2024", (-87000.0, -107000.0)),
        ("MDT-2m-114199-07-2024", (-86000.0, -101000.0)),
        ("MDT-2m-114198-07-2024", (-86000.0, -102000.0)),
        ("MDT-2m-114197-07-2024", (-86000.0, -103000.0)),
        ("LO-160467-07-2025", (-40000.0, 167000.0)),
        ("LO-159466-07-2025", (-41000.0, 166000.0)),
        ("LO-160466-07-2025", (-40000.0, 166000.0)),
        ("MDS-2m-162467-07-2025", (-38000.0, 167000.0)),
        ("MDS-2m-161467-07-2025", (-39000.0, 167000.0)),
        ("MDS-2m-162463-07-2025", (-38000.0, 163000.0)),
    ],
)
def test_tile_extent_matches_live_geotransform_or_reprojected_bbox(
    item_id: str, expected_origin: tuple[float, float]
) -> None:
    """``tile_extent_3763``'s (minx, miny, maxx, maxy) should have its
    (minx, maxy) corner exactly at (originX, originY) -- see module docstring
    for the live evidence backing this convention."""

    key = decode_tile_key(item_id)
    assert key is not None
    minx, miny, maxx, maxy = tile_extent_3763(key)
    origin_x, origin_y = expected_origin
    assert (minx, maxy) == (origin_x, origin_y)
    assert maxx == origin_x + 1000
    assert miny == origin_y - 1000


def test_same_tile_different_collections_share_origin() -> None:
    """MDT-2m-113194-07-2024 and LO-113194-07-2024 are the same physical
    tile in different collections -- their decoded origins must match."""

    a = decode_tile_key("MDT-2m-113194-07-2024")
    b = decode_tile_key("LO-113194-07-2024")
    assert a is not None and b is not None
    assert tile_extent_3763(a) == tile_extent_3763(b)


@pytest.mark.parametrize(
    "item_id",
    [
        "",
        "foo",
        "a-b-c",
        # Real orthophoto ID scheme: does not match the tile pattern at all.
        "ORTOS-2021-cog-25cm-122-4",
        "ORTOS-2021-cog-25cm-122-3",
        # 5-digit (not 6-digit) tile token.
        "MDT-2m-11319-07-2024",
        # 1-digit (not 2-digit) lot.
        "MDT-2m-113194-7-2024",
        # 2-digit (not 4-digit) year.
        "MDT-2m-113194-07-24",
        # Non-numeric tile token.
        "MDT-2m-abcdef-07-2024",
        # No prefix at all.
        "113194-07-2024",
    ],
)
def test_decode_tile_key_never_raises_on_foreign_ids(item_id: str) -> None:
    assert decode_tile_key(item_id) is None
