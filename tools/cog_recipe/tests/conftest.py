"""Synthetic Float32 GeoTIFF tile fixtures -- no network, no real DGT data.

Mimics DGT's real output closely enough to exercise the pipeline: single-band
Float32, strip-encoded (not tiled), EPSG:3763, a smooth elevation surface plus
a nodata patch, laid out on a shared 1 km-like grid so 4 tiles mosaic into a
seamless 2x2 block (analogous to `tiles.py`'s tile-id grid convention, but at
a much smaller, fast-to-test scale).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine

CRS = "EPSG:3763"
NODATA = -999.0
TILE_PX = 200
RESOLUTION = 0.5  # metres/pixel -- same as MDT-50cm
TILE_EXTENT_M = TILE_PX * RESOLUTION  # 100 m


def _elevation_surface(tx: int, ty: int, px: int) -> np.ndarray:
    """A smooth, tile-continuous elevation surface (no discontinuity at
    tile boundaries), plus a per-tile offset so tiles are visually distinct.
    """

    row = np.arange(px, dtype="float64")
    col = np.arange(px, dtype="float64")
    # Global (mosaic-wide) coordinates so adjacent tiles are continuous.
    gx = tx * px + col
    gy = ty * px + row
    surface = 100.0 + 0.01 * gx[np.newaxis, :] + 0.02 * gy[:, np.newaxis]
    surface += 3.0 * np.sin(gx[np.newaxis, :] / 40.0) * np.cos(gy[:, np.newaxis] / 40.0)
    return surface.astype("float32")


def write_tile(
    path: Path,
    *,
    tx: int,
    ty: int,
    px: int = TILE_PX,
    resolution: float = RESOLUTION,
    nodata: float | None = NODATA,
    crs: str = CRS,
    add_nodata_patch: bool = False,
) -> np.ndarray:
    """Write one synthetic tile at grid position (tx, ty) (tiles increase in
    x eastward and in y *southward*, matching GDAL's top-left-origin
    convention used throughout cddpt -- see src/cddpt/tiles.py).

    Returns the array actually written (nodata patch applied), for tests to
    compare against.
    """

    origin_x = tx * px * resolution
    origin_y = -(ty * px * resolution)  # north edge; tiles extend south (-y)
    transform = Affine(resolution, 0, origin_x, 0, -resolution, origin_y)

    data = _elevation_surface(tx, ty, px)
    if add_nodata_patch:
        data = data.copy()
        mid = px // 2
        data[mid - 10 : mid + 10, mid - 10 : mid + 10] = nodata if nodata is not None else -999.0

    profile = {
        "driver": "GTiff",
        "height": px,
        "width": px,
        "count": 1,
        "dtype": "float32",
        "crs": crs,
        "transform": transform,
        # Strip-encoded (not tiled), like DGT's real source rasters.
        "tiled": False,
    }
    if nodata is not None:
        profile["nodata"] = nodata

    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
    return data


@pytest.fixture
def tile_grid_2x2(tmp_path: Path) -> tuple[Path, dict[tuple[int, int], np.ndarray]]:
    """4 adjacent 200x200 tiles (2x2), one with a nodata patch."""

    input_dir = tmp_path / "tiles"
    input_dir.mkdir()
    arrays: dict[tuple[int, int], np.ndarray] = {}
    for ty in range(2):
        for tx in range(2):
            path = input_dir / f"MDT-50cm-{200 + tx:03d}{300 + ty:03d}-07-2025.tif"
            arrays[(tx, ty)] = write_tile(
                path, tx=tx, ty=ty, add_nodata_patch=(tx == 0 and ty == 0)
            )
    return input_dir, arrays


@pytest.fixture
def single_tile(tmp_path: Path) -> tuple[Path, Path, np.ndarray]:
    """One tile in its own input dir; returns (input_dir, tile_path, array)."""

    input_dir = tmp_path / "single"
    input_dir.mkdir()
    path = input_dir / "MDT-50cm-200300-07-2025.tif"
    array = write_tile(path, tx=0, ty=0, add_nodata_patch=True)
    return input_dir, path, array
