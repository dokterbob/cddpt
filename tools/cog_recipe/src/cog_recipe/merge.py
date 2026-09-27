"""Stage: seamless mosaic -> native-resolution COG with a guaranteed overview.

This is the primary command (``build-cog merge``). Per-tile overviews are
power-of-2 decimations of a single 1 km tile, baking seams into any
resampled product at every tile boundary; merging first and decimating the
*merged* raster removes them. See docs/build-cog.md "Why re-encode" for the
LERC/MAX_Z_ERROR rationale and README.md for the pipeline write-up.

Pipeline, all via `rasterio`'s bundled GDAL (see ``gdal_support.py`` /
``vrt.py`` for why no ``osgeo``):

1. Scan + validate the input tiles form one mosaicable grid (``tileset.py``).
2. Write a mosaic VRT over their union (or a ``--bbox`` clip snapped to the
   ``--ensure-overview-res`` grid, ``grid.py``) -- a lazy, seamless view;
   no pixel data is read yet.
3. Stream-copy that VRT into a base GeoTIFF at native resolution (GDAL's
   `CreateCopy`, or a manual block loop when clipping to a sub-window --
   either way, bounded per-block memory, never the whole mosaic in numpy).
4. `Dataset.build_overviews()` with an explicit factor list
   (``overviews.py``) that includes the exact factor reaching
   ``--ensure-overview-res``, using ``average`` resampling on the *merged*
   raster -- so that level is seamless across former tile boundaries.
5. Re-encode as a proper COG via GDAL's COG driver with
   ``OVERVIEWS=FORCE_USE_EXISTING`` (verified empirically: this preserves
   our custom, non-power-of-2 factor list instead of the driver
   recomputing its own power-of-2-only pyramid).
6. Validate, atomically replace the output, clean up intermediates.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import rasterio
import rasterio.shutil as rio_shutil
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.windows import Window

from cog_recipe.atomic import cleanup_stray_tmp, tmp_path_for
from cog_recipe.gdal_support import check_lerc_zstd_support
from cog_recipe.grid import Bounds, clamp, snap_outward, union_bounds
from cog_recipe.overviews import compute_overview_factors
from cog_recipe.progress import ProgressCallback, ProgressEvent, report
from cog_recipe.tileset import load_tile_set, scan_tiles
from cog_recipe.validate import validate_cog
from cog_recipe.vrt import build_mosaic_vrt

DEFAULT_MAX_Z_ERROR = 0.05
DEFAULT_ENSURE_OVERVIEW_RES = 10.0
DEFAULT_BLOCKSIZE = 512
DEFAULT_COMPRESS = "lerc_zstd"
DEFAULT_OVERVIEW_RESAMPLING = "average"


@dataclass(frozen=True, slots=True)
class MergeResult:
    """Summary of a completed :func:`merge_tiles` run."""

    output_path: Path
    tile_count: int
    width: int
    height: int
    resolution: float
    bounds: Bounds
    overview_factors: list[int]
    ensure_overview_res: float
    output_bytes: int


def merge_tiles(
    input_dir: Path,
    output_path: Path,
    *,
    pattern: str = "*.tif",
    bbox: Bounds | None = None,
    max_z_error: float = DEFAULT_MAX_Z_ERROR,
    nodata: float | None = None,
    ensure_overview_res: float | None = DEFAULT_ENSURE_OVERVIEW_RES,
    compress: str = DEFAULT_COMPRESS,
    blocksize: int = DEFAULT_BLOCKSIZE,
    overview_resampling: str = DEFAULT_OVERVIEW_RESAMPLING,
    keep_intermediate: bool = False,
    gdal_num_threads: str = "ALL_CPUS",
    progress: ProgressCallback | None = None,
) -> MergeResult:
    """Build one seamless COG mosaic from the GeoTIFF tiles in ``input_dir``.

    ``ensure_overview_res=None`` skips the guaranteed-overview machinery
    entirely (plain power-of-2 pyramid); the default guarantees a 10 m
    level for `gdal_contour` (see README).
    """

    check_lerc_zstd_support()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cleanup_stray_tmp(output_path.parent)

    report(progress, ProgressEvent("scan", 0, 0, f"scanning {input_dir}"))
    paths = scan_tiles(input_dir, pattern)
    if not paths:
        raise ValueError(f"no tiles matching {pattern!r} found in {input_dir}")
    tile_set = load_tile_set(paths, nodata_override=nodata)
    report(progress, ProgressEvent("scan", len(paths), len(paths), f"{len(paths)} tiles"))

    mosaic_bounds = union_bounds([t.bounds for t in tile_set.tiles])
    clip_bounds = _resolve_clip_bounds(bbox, mosaic_bounds, ensure_overview_res)

    resolution = tile_set.resolution
    width = round((clip_bounds[2] - clip_bounds[0]) / resolution)
    height = round((clip_bounds[3] - clip_bounds[1]) / resolution)

    factors = (
        compute_overview_factors(resolution, ensure_overview_res, width, height, stop_dim=blocksize)
        if ensure_overview_res is not None
        else _power_of_two_factors(width, height, blocksize)
    )
    ensure_factor = (
        round(ensure_overview_res / resolution) if ensure_overview_res is not None else None
    )
    guarantee_needed = ensure_factor is not None and ensure_factor > 1
    required_overview_res: list[float] = (
        [ensure_overview_res] if guarantee_needed and ensure_overview_res is not None else []
    )

    vrt_path = output_path.with_name(output_path.name + ".mosaic.vrt")
    base_path = output_path.with_name(output_path.name + ".base.tmp.tif")
    final_tmp = tmp_path_for(output_path)

    try:
        with rasterio.Env(GDAL_NUM_THREADS=gdal_num_threads):
            report(progress, ProgressEvent("vrt", 0, 1, "building mosaic VRT"))
            build_mosaic_vrt(
                tile_set.tiles,
                vrt_path,
                crs_wkt=tile_set.crs.to_wkt(),
                dtype=tile_set.dtype,
                pixel_size_x=resolution,
                pixel_size_y=resolution,
                nodata=tile_set.nodata,
            )
            report(progress, ProgressEvent("vrt", 1, 1, str(vrt_path)))

            report(progress, ProgressEvent("base-write", 0, 1, "streaming base raster"))
            _write_base_raster(
                vrt_path,
                base_path,
                mosaic_bounds=mosaic_bounds,
                clip_bounds=clip_bounds,
                resolution=resolution,
                width=width,
                height=height,
                nodata=tile_set.nodata,
                compress=compress,
                blocksize=blocksize,
                crs=tile_set.crs,
                progress=progress,
            )
            report(progress, ProgressEvent("base-write", 1, 1, str(base_path)))

            report(progress, ProgressEvent("overviews", 0, 1, f"factors={factors}"))
            resampling_enum = Resampling[overview_resampling]
            with rasterio.open(base_path, "r+") as base_ds:
                # `factors` (see overviews.py) is built so every entry is an
                # exact power-of-2 multiple of its predecessor -- GDAL
                # cascades each new overview level from the nearest existing
                # one in a single build_overviews() call, and only an exact
                # integer ratio keeps that cascade mathematically identical
                # to averaging directly from the full-resolution source.
                base_ds.build_overviews(factors, resampling_enum)
            report(progress, ProgressEvent("overviews", 1, 1, ""))

            report(progress, ProgressEvent("cog-encode", 0, 1, str(output_path)))
            with rasterio.open(base_path) as base_ds:
                rio_shutil.copy(
                    base_ds,
                    final_tmp,
                    driver="COG",
                    COMPRESS=compress.upper(),
                    MAX_Z_ERROR=max_z_error,
                    BLOCKSIZE=blocksize,
                    OVERVIEWS="FORCE_USE_EXISTING",
                    RESAMPLING=overview_resampling.upper(),
                    BIGTIFF="IF_SAFER",
                    NUM_THREADS=gdal_num_threads,
                )
            report(progress, ProgressEvent("cog-encode", 1, 1, ""))

            report(progress, ProgressEvent("validate", 0, 1, ""))
            validate_cog(
                final_tmp,
                expected_crs=tile_set.crs,
                expected_nodata=tile_set.nodata,
                expected_dtype=tile_set.dtype,
                expected_size=(width, height),
                required_overview_resolutions=required_overview_res,
            )
            report(progress, ProgressEvent("validate", 1, 1, ""))

        os.replace(final_tmp, output_path)
    except BaseException:
        if final_tmp.exists():
            final_tmp.unlink()
        raise
    finally:
        if not keep_intermediate:
            base_path.unlink(missing_ok=True)
            vrt_path.unlink(missing_ok=True)

    return MergeResult(
        output_path=output_path,
        tile_count=len(tile_set.tiles),
        width=width,
        height=height,
        resolution=resolution,
        bounds=clip_bounds,
        overview_factors=factors,
        ensure_overview_res=ensure_overview_res if ensure_overview_res is not None else resolution,
        output_bytes=output_path.stat().st_size,
    )


def _resolve_clip_bounds(
    bbox: Bounds | None,
    mosaic_bounds: Bounds,
    ensure_overview_res: float | None,
) -> Bounds:
    if bbox is None:
        return mosaic_bounds
    grid_res = ensure_overview_res if ensure_overview_res is not None else 1.0
    snapped = snap_outward(bbox, grid_res)
    return clamp(snapped, mosaic_bounds)


def _power_of_two_factors(width: int, height: int, stop_dim: int) -> list[int]:
    max_dim = max(width, height)
    factors: list[int] = []
    f = 2
    while True:
        factors.append(f)
        if max_dim // f <= stop_dim:
            break
        f *= 2
    return factors


def _write_base_raster(
    vrt_path: Path,
    base_path: Path,
    *,
    mosaic_bounds: Bounds,
    clip_bounds: Bounds,
    resolution: float,
    width: int,
    height: int,
    nodata: float,
    compress: str,
    blocksize: int,
    crs: CRS,
    progress: ProgressCallback | None,
) -> None:
    """Stream the (optionally clipped) mosaic into a compressed base GeoTIFF.

    Full-extent case: a single GDAL `CreateCopy` (streamed block-by-block at
    the C level, never the whole raster in a numpy array). Clipped case: a
    manual per-block loop bounded to one block's worth of memory at a time.
    """

    no_clip = clip_bounds == mosaic_bounds
    with rasterio.open(vrt_path) as mosaic_ds:
        if no_clip:
            rio_shutil.copy(
                mosaic_ds,
                base_path,
                driver="GTiff",
                tiled=True,
                blockxsize=blocksize,
                blockysize=blocksize,
                compress=compress.upper(),
                nodata=nodata,
                BIGTIFF="IF_SAFER",
            )
            return

        col_off = round((clip_bounds[0] - mosaic_bounds[0]) / resolution)
        row_off = round((mosaic_bounds[3] - clip_bounds[3]) / resolution)
        src_origin = Window(col_off=col_off, row_off=row_off, width=width, height=height)

        transform = rasterio.transform.from_origin(
            clip_bounds[0], clip_bounds[3], resolution, resolution
        )
        with rasterio.open(
            base_path,
            "w",
            driver="GTiff",
            width=width,
            height=height,
            count=1,
            dtype=mosaic_ds.dtypes[0],
            crs=crs,
            transform=transform,
            nodata=nodata,
            tiled=True,
            blockxsize=blocksize,
            blockysize=blocksize,
            compress=compress.upper(),
            BIGTIFF="IF_SAFER",
        ) as dst:
            block_windows = list(dst.block_windows(1))
            total = len(block_windows)
            for i, (_, window) in enumerate(block_windows):
                src_window = Window(
                    col_off=src_origin.col_off + window.col_off,
                    row_off=src_origin.row_off + window.row_off,
                    width=window.width,
                    height=window.height,
                )
                data = mosaic_ds.read(1, window=src_window)
                dst.write(data, 1, window=window)
                if i % max(1, total // 20) == 0:
                    report(progress, ProgressEvent("base-write", i, total, ""))
