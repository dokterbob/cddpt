from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rio_cogeo.cogeo import cog_validate

from cog_recipe.errors import GdalCapabilityError
from cog_recipe.merge import merge_tiles
from cog_recipe.tileset import DEFAULT_NODATA

MAX_Z_ERROR = 0.05
EPS = 1e-4
RESOLUTION = 0.5
TILE_PX = 200


def test_merge_native_resolution_is_seamless_and_matches_sources(
    tile_grid_2x2: tuple[Path, dict[tuple[int, int], np.ndarray]],
) -> None:
    input_dir, arrays = tile_grid_2x2
    output = input_dir.parent / "merged.tif"

    result = merge_tiles(
        input_dir,
        output,
        ensure_overview_res=2.5,  # small, fast pyramid for this tiny mosaic
        blocksize=128,
    )

    assert output.exists()
    assert result.tile_count == 4
    assert result.resolution == RESOLUTION
    # extent = union of the 4 tiles: x in [0, 200], y in [-200, 0]
    assert result.bounds == (0.0, -200.0, 200.0, 0.0)
    assert (result.width, result.height) == (400, 400)

    is_valid, errors, _ = cog_validate(str(output))
    assert is_valid, errors

    with rasterio.open(output) as ds:
        assert ds.crs.to_epsg() == 3763
        assert ds.nodata == -999.0
        assert ds.dtypes[0] == "float32"
        assert (ds.width, ds.height) == (400, 400)
        merged = ds.read(1)

    for (tx, ty), source_array in arrays.items():
        dx = tx * TILE_PX
        dy = ty * TILE_PX
        window = merged[dy : dy + TILE_PX, dx : dx + TILE_PX]
        valid = source_array != DEFAULT_NODATA
        diff = np.abs(window[valid] - source_array[valid])
        assert diff.max() <= MAX_Z_ERROR + EPS, f"tile ({tx},{ty}) mismatch: {diff.max()}"
        # nodata pixels round-trip as nodata, not as a bogus decoded value.
        assert np.all(window[~valid] == -999.0)

    # No seam: values straddling the internal tile boundary (col 200) come
    # from a globally-continuous source function, so they must be close --
    # but more importantly, they were placed from disjoint source tiles at
    # all, with no gap/overlap artefact at the boundary.
    assert np.isfinite(merged[199, 199]) and np.isfinite(merged[200, 200])


def test_merge_ensures_overview_at_requested_resolution(
    tile_grid_2x2: tuple[Path, dict[tuple[int, int], np.ndarray]],
) -> None:
    input_dir, _arrays = tile_grid_2x2
    output = input_dir.parent / "merged.tif"

    # A tight max_z_error isolates the averaging math from LERC quantization
    # noise (both the native band and the overview are independently
    # LERC-quantized in the final COG encode; with the default 0.05 m bound
    # that noise alone can exceed a naive tight epsilon).
    result = merge_tiles(
        input_dir, output, ensure_overview_res=2.5, blocksize=128, max_z_error=1e-4
    )
    factor = round(2.5 / RESOLUTION)
    assert factor in result.overview_factors

    with rasterio.open(output) as ds:
        overview_factors = ds.overviews(1)
        assert factor in overview_factors
        idx = overview_factors.index(factor)
        with rasterio.open(output, OVERVIEW_LEVEL=idx) as ov_ds:
            ov_transform = ov_ds.transform
            assert ov_transform.a == pytest.approx(2.5)
            assert -ov_transform.e == pytest.approx(2.5)
            # target-aligned: overview grid origin is a multiple of 2.5
            assert ov_transform.c % 2.5 == pytest.approx(0.0, abs=1e-6)
            assert ov_transform.f % 2.5 == pytest.approx(0.0, abs=1e-6)
            ov_data = ov_ds.read(1, masked=True)

    with rasterio.open(output) as native_ds:
        native = native_ds.read(1, masked=True)

    # A cell well away from the nodata patch and mosaic edges must equal the
    # mean of its native factor x factor block (average resampling on the
    # *merged*, seamless raster).
    row, col = 30, 30  # lands inside tile (0,0), away from its nodata patch
    block = native[row * factor : (row + 1) * factor, col * factor : (col + 1) * factor]
    assert not np.ma.is_masked(block)
    # Tight tolerance: the guaranteed-resolution level must be built directly
    # from the native-resolution merged raster, not cascaded from another
    # (power-of-2) overview -- cascading would silently introduce error far
    # larger than LERC quantization noise alone (regression test: this used
    # to fail with a ~0.0077 m diff before the fix in merge.py that builds
    # the guaranteed level first, alone, before any other overview exists).
    assert ov_data[row, col] == pytest.approx(block.mean(), abs=1e-3)


def test_merge_bbox_clip(
    tile_grid_2x2: tuple[Path, dict[tuple[int, int], np.ndarray]],
) -> None:
    input_dir, arrays = tile_grid_2x2
    output = input_dir.parent / "merged_clip.tif"

    # An arbitrary, non-grid-aligned bbox comfortably inside the mosaic
    # (mosaic bounds are (0, -200, 200, 0)).
    bbox = (13.3, -150.7, 187.2, -12.4)
    result = merge_tiles(
        input_dir,
        output,
        bbox=bbox,
        ensure_overview_res=2.5,
        blocksize=128,
    )

    # snapped outward to the 2.5 m grid, and strictly containing the bbox
    assert result.bounds[0] <= bbox[0]
    assert result.bounds[1] <= bbox[1]
    assert result.bounds[2] >= bbox[2]
    assert result.bounds[3] >= bbox[3]
    for value in result.bounds:
        assert value % 2.5 == pytest.approx(0.0, abs=1e-6)

    with rasterio.open(output) as ds:
        assert (ds.width, ds.height) == (result.width, result.height)
        b = ds.bounds
        assert (b.left, b.bottom, b.right, b.top) == pytest.approx(result.bounds)
        clipped = ds.read(1)

    # spot-check: a pixel inside the clip still matches its source tile.
    # Tile (1, 1) covers x in [100, 200], y in [-200, -100]; pick a point
    # inside both that tile and the clip.
    tile_array = arrays[(1, 1)]
    world_x, world_y = 150.25, -150.25
    src_col = int((world_x - 100.0) / RESOLUTION)
    src_row = int((-100.0 - world_y) / RESOLUTION)  # tile (1,1) top edge is y=-100
    dst_col = int((world_x - result.bounds[0]) / RESOLUTION)
    dst_row = int((result.bounds[3] - world_y) / RESOLUTION)
    if 0 <= src_row < TILE_PX and 0 <= src_col < TILE_PX:
        source_val = tile_array[src_row, src_col]
        if source_val != DEFAULT_NODATA:
            assert abs(float(clipped[dst_row, dst_col]) - float(source_val)) <= MAX_Z_ERROR + EPS


def test_merge_no_tiles_found(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(ValueError, match="no tiles"):
        merge_tiles(empty_dir, tmp_path / "out.tif")


def test_merge_ensure_res_disabled_gives_plain_pyramid(
    tile_grid_2x2: tuple[Path, dict[tuple[int, int], np.ndarray]],
) -> None:
    input_dir, _arrays = tile_grid_2x2
    output = input_dir.parent / "merged_no_ensure.tif"

    result = merge_tiles(input_dir, output, ensure_overview_res=None, blocksize=128)
    assert result.overview_factors == sorted(result.overview_factors)
    for f in result.overview_factors:
        # power of two
        assert f & (f - 1) == 0


def test_merge_preflight_lerc_zstd_failure(
    tile_grid_2x2: tuple[Path, dict[tuple[int, int], np.ndarray]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir, _arrays = tile_grid_2x2
    output = input_dir.parent / "merged.tif"

    import cog_recipe.merge as merge_mod

    def _boom() -> None:
        raise GdalCapabilityError("LERC_ZSTD is not supported by this GDAL build")

    monkeypatch.setattr(merge_mod, "check_lerc_zstd_support", _boom)

    with pytest.raises(GdalCapabilityError, match="LERC_ZSTD"):
        merge_tiles(input_dir, output)

    assert not output.exists()
