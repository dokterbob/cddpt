"""Scanning and validating a directory of input GeoTIFF tiles.

Opening ~91,000 tiles sequentially (one ``rasterio.open()`` + a handful of
Python-level checks each) is I/O-bound at roughly 150 files/s -- about 10
minutes for a national MDT-50cm run before any mosaic work starts.
`rasterio.open()` is a thin wrapper over a GDAL C call that releases the
GIL for its duration, so a thread pool (:func:`load_tile_set`,
:func:`scan_tile_bounds`) parallelises the wall-clock I/O wait even though
Python itself stays single-threaded for the (cheap) per-tile bookkeeping.
``fast_validate`` trims the remaining Python-side cost further: full
CRS/dtype/pixel-size/rotation cross-checks only run on a sample of tiles,
trusting the rest to share the sampled tiles' grid -- a reasonable bet for a
directory of consistently-named DGT tiles, but not a substitute for the full
check on a source of unknown provenance.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import rasterio
from rasterio.crs import CRS

from cog_recipe.errors import TileGridError
from cog_recipe.grid import Bounds

#: Applied when no source tile declares a nodata value (see docs/build-cog.md
#: "Why re-encode": the design's documented, disclosed default).
DEFAULT_NODATA = -999.0

#: Default number of tiles fully cross-validated by ``fast_validate`` (first,
#: last, and an even spread in between) -- enough to catch a systematically
#: mismatched subset without paying full per-tile validation cost across
#: tens of thousands of tiles.
DEFAULT_SAMPLE_SIZE = 200

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


def _default_scan_workers() -> int:
    """~4x CPU count, capped at 32 -- this is an I/O-bound workload (disk/
    network latency per file open), so oversubscribing real cores is
    intentional; the cap avoids runaway thread counts on very large boxes.
    """

    cpu = os.cpu_count() or 4
    return min(32, max(4, cpu * 4))


@dataclass(frozen=True, slots=True)
class _RawTile:
    """One tile's header fields, read with no cross-tile validation yet."""

    path: Path
    width: int
    height: int
    bounds: Bounds
    pixel_size_x: float
    pixel_size_y: float
    crs: CRS
    dtype: str
    band_count: int
    rotated: bool
    nodata: float | None


def _read_tile_header(path: Path) -> _RawTile:
    with rasterio.open(path) as src:
        transform = src.transform
        return _RawTile(
            path=path,
            width=src.width,
            height=src.height,
            bounds=tuple(src.bounds),
            pixel_size_x=transform.a,
            pixel_size_y=-transform.e,
            crs=src.crs,
            dtype=src.dtypes[0],
            band_count=src.count,
            rotated=transform.b != 0 or transform.d != 0,
            nodata=src.nodata,
        )


def _read_tile_bounds(path: Path) -> Bounds:
    with rasterio.open(path) as src:
        return tuple(src.bounds)


def _sample_indices(n: int, sample_size: int) -> set[int]:
    """Indices to fully validate under ``fast_validate``: first, last, and an
    even spread of roughly ``sample_size`` tiles in between.
    """

    if n <= sample_size:
        return set(range(n))
    step = max(1, n // sample_size)
    indices = set(range(0, n, step))
    indices.add(0)
    indices.add(n - 1)
    return indices


def scan_tile_bounds(paths: list[Path], *, max_workers: int | None = None) -> list[Bounds]:
    """Read just each tile's bounds, in parallel.

    The minimum needed to compute a mosaic's overall extent (e.g. for
    ``build-cog plan-regions``) without :func:`load_tile_set`'s full
    cross-tile validation.
    """

    workers = max_workers if max_workers is not None else _default_scan_workers()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_read_tile_bounds, paths))


def load_tile_set(
    paths: list[Path],
    *,
    nodata_override: float | None = None,
    max_workers: int | None = None,
    fast_validate: bool = False,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> TileSet:
    """Open and validate every path in ``paths``, returning a :class:`TileSet`.

    Tiles are opened concurrently in a thread pool (``max_workers``, default
    :func:`_default_scan_workers`) -- `rasterio.open()` releases the GIL for
    its GDAL call, so this parallelises the I/O wait even under CPython.

    Raises :class:`TileGridError` if the tiles don't share a CRS, dtype, or
    pixel size -- a prerequisite for building a single seamless mosaic. With
    ``fast_validate=True``, that cross-tile check only runs on a sample of
    tiles (``sample_size``, see :func:`_sample_indices`) rather than every
    one -- a disclosed trade-off, safe only when the input directory is
    trusted to already be a consistent DGT tile set (band count is still
    checked on every tile regardless: it's a cheap, purely local check with
    no cross-tile comparison).
    """

    if not paths:
        raise TileGridError("no input tiles found")

    workers = max_workers if max_workers is not None else _default_scan_workers()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        raw_tiles: list[_RawTile] = list(pool.map(_read_tile_header, paths))

    validated_indices: Iterable[int] = (
        _sample_indices(len(raw_tiles), sample_size) if fast_validate else range(len(raw_tiles))
    )
    validated_indices = set(validated_indices)

    tiles: list[TileInfo] = []
    crs: CRS | None = None
    dtype: str | None = None
    res_x: float | None = None
    res_y: float | None = None
    nodata_values: set[float] = set()
    tiles_missing_nodata = 0

    for i, raw in enumerate(raw_tiles):
        if raw.band_count != 1:
            raise TileGridError(
                f"{raw.path}: expected a single-band raster, found {raw.band_count}"
            )

        if crs is None:
            crs = raw.crs
            dtype = raw.dtype
            res_x, res_y = raw.pixel_size_x, raw.pixel_size_y
        elif i in validated_indices:
            if raw.crs != crs:
                raise TileGridError(f"{raw.path}: CRS {raw.crs} != {crs} (first tile)")
            if raw.dtype != dtype:
                raise TileGridError(f"{raw.path}: dtype {raw.dtype} != {dtype} (first tile)")
            assert res_x is not None and res_y is not None
            if (
                abs(raw.pixel_size_x - res_x) > _RES_TOLERANCE
                or abs(raw.pixel_size_y - res_y) > _RES_TOLERANCE
            ):
                raise TileGridError(
                    f"{raw.path}: pixel size ({raw.pixel_size_x}, {raw.pixel_size_y}) != "
                    f"({res_x}, {res_y}) (first tile)"
                )

        if i in validated_indices and raw.rotated:
            raise TileGridError(f"{raw.path}: rotated/sheared geotransform not supported")

        if raw.nodata is None:
            tiles_missing_nodata += 1
        else:
            nodata_values.add(float(raw.nodata))

        tiles.append(
            TileInfo(
                path=raw.path,
                width=raw.width,
                height=raw.height,
                bounds=raw.bounds,
                pixel_size_x=raw.pixel_size_x,
                pixel_size_y=raw.pixel_size_y,
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
