"""Decoding for CDD's reverse-engineered tile ID scheme.

Tile IDs (e.g. ``LO-114202-07-2024``, ``MDT-2m-114202-07-2024``) follow the
undocumented pattern::

    {prefix}-{tileX:03d}{tileY:03d}-{lot:02d}-{year}

where ``originX = (tileX - 200) * 1000`` and ``originY = (tileY - 300) *
1000`` in EPSG:3763 (Portugal TM06), each tile being 1 km square. Prefixes
may themselves contain hyphens (``MDT-2m``, ``MDS-50cm``), so
:func:`decode_tile_key` parses from the *right*: the last three ``-``
separated tokens are always ``{tileXtileY}-{lot}-{year}``, and whatever
remains (rejoined with ``-``) is the prefix.

Origin convention (empirically verified against live data -- see
``tests/test_tiles.py`` for the full evidence table)
-----------------------------------------------------------------------------
For raster items whose corrupted ``bbox`` *is* the literal GDAL geotransform
(``[originX, pixelWidth, rowRotation, originY, colRotation, pixelHeight]``,
see ``catalog.py``'s module docstring), ``bbox[0]`` and ``bbox[3]`` match
``originX``/``originY`` computed from the tile ID *exactly* -- e.g.
``MDT-2m-113194-07-2024`` has ``bbox == [-87000, 2, 0, -106000, 0, -2]``,
and ``(113 - 200) * 1000 == -87000``, ``(194 - 300) * 1000 == -106000``.
Since ``pixelHeight`` is negative (``-2``), this is the standard GDAL
top-left-origin convention: ``originY`` is the tile's **top (north, maximum
Y) edge**, not its bottom edge. This was cross-checked against several LAZ
items (whose ``bbox`` is a genuine WGS84 bbox, never corrupted) reprojected
to EPSG:3763: e.g. ``LO-113194-07-2024``'s real bbox reprojects to
approximately ``x: [-87011, -86000], y: [-106999, -105989]`` -- i.e. an
origin at ``(-87000, -106000)`` with the tile extending *down* (south, to
``y=-107000``) and *right* (east, to ``x=-86000``) from it. So the full tile
extent is ``[originX, originY - 1000, originX + 1000, originY]``.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Tiles are 1 km squares in EPSG:3763.
_TILE_SIZE_M = 1000.0
#: See module docstring: origin formulas.
_TILE_X_OFFSET = 200
_TILE_Y_OFFSET = 300


@dataclass(frozen=True, slots=True)
class TileKey:
    """A decoded CDD tile ID.

    ``item_id`` retains the original, undecoded item ID for traceability.
    """

    item_id: str
    prefix: str
    tile_x: int
    tile_y: int
    lot: int
    year: int


def decode_tile_key(item_id: str) -> TileKey | None:
    """Decode ``item_id`` into a :class:`TileKey`, or ``None`` if it does not
    match CDD's tile ID scheme.

    Never raises: an item ID from an unrelated/foreign scheme (e.g.
    orthophoto IDs like ``ORTOS-2021-cog-25cm-122-4``) simply yields
    ``None``. Discovery of downloadable data must stay geometry-driven; this
    is for naming/grouping only.
    """

    parts = item_id.split("-")
    if len(parts) < 4:
        return None

    year_str, lot_str, tile_str = parts[-1], parts[-2], parts[-3]
    prefix = "-".join(parts[:-3])

    if not prefix:
        return None
    if not (year_str.isdigit() and len(year_str) == 4):
        return None
    if not (lot_str.isdigit() and len(lot_str) == 2):
        return None
    if not (tile_str.isdigit() and len(tile_str) == 6):
        return None

    return TileKey(
        item_id=item_id,
        prefix=prefix,
        tile_x=int(tile_str[:3]),
        tile_y=int(tile_str[3:]),
        lot=int(lot_str),
        year=int(year_str),
    )


def tile_extent_3763(key: TileKey) -> tuple[float, float, float, float]:
    """The tile's 1km x 1km extent in EPSG:3763, as ``(minx, miny, maxx, maxy)``.

    See the module docstring for the empirical basis of the origin
    convention: ``originY`` is the tile's top (maximum-Y) edge.
    """

    origin_x = (key.tile_x - _TILE_X_OFFSET) * _TILE_SIZE_M
    origin_y = (key.tile_y - _TILE_Y_OFFSET) * _TILE_SIZE_M
    return (origin_x, origin_y - _TILE_SIZE_M, origin_x + _TILE_SIZE_M, origin_y)


__all__ = ["TileKey", "decode_tile_key", "tile_extent_3763"]
