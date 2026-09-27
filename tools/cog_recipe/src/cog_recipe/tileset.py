"""Scanning and validating a directory of input GeoTIFF tiles."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path

import rasterio
from rasterio.crs import CRS

from cog_recipe.errors import TileGridError
from cog_recipe.grid import Bounds

#: Applied when no source tile declares a nodata value (see docs/build-cog.md
#: "Why re-encode": the design's documented, disclosed default).
DEFAULT_NODATA = -999.0

_RES_TOLERANCE = 1e-9


@dataclass(frozen=True, slots=True)
class TileInfo:
    """One input tile's georeferencing, as needed to place it in a mosaic."""

    path: Path
    width: int
    height: int
    bounds: Bounds
    pixel_size_x: float
    pixel_size_y: float


@dataclass(frozen=True, slots=True)
class TileSet:
    """A validated, mosaicable collection of tiles.

    All tiles share ``crs``, ``dtype``, pixel size (``resolution``), and an
    effective ``nodata`` value (the common source nodata, or
    :data:`DEFAULT_NODATA` with a warning if none of the tiles declare one).
    """

    tiles: list[TileInfo]
    crs: CRS
    dtype: str
    resolution: float
    nodata: float


def scan_tiles(input_dir: Path, pattern: str = "*.tif") -> list[Path]:
    """Sorted list of files under ``input_dir`` matching ``pattern``."""

    return sorted(input_dir.glob(pattern))


def load_tile_set(
    paths: list[Path],
    *,
    nodata_override: float | None = None,
) -> TileSet:
    """Open and validate every path in ``paths``, returning a :class:`TileSet`.

    Raises :class:`TileGridError` if the tiles don't share a CRS, dtype, or
    pixel size -- a prerequisite for building a single seamless mosaic.
    """

    if not paths:
        raise TileGridError("no input tiles found")

    tiles: list[TileInfo] = []
    crs: CRS | None = None
    dtype: str | None = None
    res_x: float | None = None
    res_y: float | None = None
    nodata_values: set[float] = set()
    tiles_missing_nodata = 0

    for path in paths:
        with rasterio.open(path) as src:
            if src.count != 1:
                raise TileGridError(f"{path}: expected a single-band raster, found {src.count}")
            transform = src.transform
            px, py = transform.a, -transform.e
            if crs is None:
                crs = src.crs
                dtype = src.dtypes[0]
                res_x, res_y = px, py
            else:
                if src.crs != crs:
                    raise TileGridError(f"{path}: CRS {src.crs} != {crs} (first tile)")
                if src.dtypes[0] != dtype:
                    raise TileGridError(f"{path}: dtype {src.dtypes[0]} != {dtype} (first tile)")
                assert res_x is not None and res_y is not None
                if abs(px - res_x) > _RES_TOLERANCE or abs(py - res_y) > _RES_TOLERANCE:
                    raise TileGridError(
                        f"{path}: pixel size ({px}, {py}) != ({res_x}, {res_y}) (first tile)"
                    )
            if transform.b != 0 or transform.d != 0:
                raise TileGridError(f"{path}: rotated/sheared geotransform not supported")

            if src.nodata is None:
                tiles_missing_nodata += 1
            else:
                nodata_values.add(float(src.nodata))

            tiles.append(
                TileInfo(
                    path=path,
                    width=src.width,
                    height=src.height,
                    bounds=tuple(src.bounds),
                    pixel_size_x=px,
                    pixel_size_y=py,
                )
            )

    assert crs is not None and dtype is not None and res_x is not None

    nodata = _resolve_nodata(nodata_values, tiles_missing_nodata, len(tiles), nodata_override)

    return TileSet(tiles=tiles, crs=crs, dtype=dtype, resolution=res_x, nodata=nodata)


def _resolve_nodata(
    nodata_values: set[float],
    tiles_missing_nodata: int,
    tile_count: int,
    override: float | None,
) -> float:
    if override is not None:
        return override
    if len(nodata_values) > 1:
        raise TileGridError(
            f"tiles declare inconsistent nodata values {sorted(nodata_values)}; pass "
            "--nodata explicitly to override"
        )
    if len(nodata_values) == 1 and tiles_missing_nodata == 0:
        return next(iter(nodata_values))
    if len(nodata_values) == 1 and tiles_missing_nodata > 0:
        raise TileGridError(
            f"{tiles_missing_nodata}/{tile_count} tiles have no nodata set while others "
            f"declare {next(iter(nodata_values))}; pass --nodata explicitly to resolve"
        )
    warnings.warn(
        f"none of the {tile_count} input tiles declare a nodata value; defaulting to "
        f"{DEFAULT_NODATA} (pass --nodata to override)",
        stacklevel=2,
    )
    return DEFAULT_NODATA
