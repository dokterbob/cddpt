from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_tile

from cog_recipe.errors import TileGridError
from cog_recipe.tileset import DEFAULT_NODATA, load_tile_set, scan_tile_bounds, scan_tiles


def test_missing_nodata_defaults_with_warning(tmp_path: Path) -> None:
    input_dir = tmp_path / "tiles"
    input_dir.mkdir()
    write_tile(input_dir / "a.tif", tx=0, ty=0, nodata=None)
    write_tile(input_dir / "b.tif", tx=1, ty=0, nodata=None)

    with pytest.warns(UserWarning, match="nodata value"):
        tile_set = load_tile_set(scan_tiles(input_dir))

    assert tile_set.nodata == DEFAULT_NODATA


def test_nodata_override_takes_precedence(tmp_path: Path) -> None:
    input_dir = tmp_path / "tiles"
    input_dir.mkdir()
    write_tile(input_dir / "a.tif", tx=0, ty=0, nodata=-1.0)

    tile_set = load_tile_set(scan_tiles(input_dir), nodata_override=-42.0)
    assert tile_set.nodata == -42.0


def test_mismatched_crs_raises(tmp_path: Path) -> None:
    input_dir = tmp_path / "tiles"
    input_dir.mkdir()
    write_tile(input_dir / "a.tif", tx=0, ty=0)
    write_tile(input_dir / "b.tif", tx=1, ty=0, crs="EPSG:4326")

    with pytest.raises(TileGridError, match="CRS"):
        load_tile_set(scan_tiles(input_dir))


def test_mismatched_resolution_raises(tmp_path: Path) -> None:
    input_dir = tmp_path / "tiles"
    input_dir.mkdir()
    write_tile(input_dir / "a.tif", tx=0, ty=0, resolution=0.5)
    write_tile(input_dir / "b.tif", tx=1, ty=0, resolution=2.0)

    with pytest.raises(TileGridError, match="pixel size"):
        load_tile_set(scan_tiles(input_dir))


def test_parallel_scan_matches_sequential_result(
    tile_grid_2x2: tuple[Path, dict[tuple[int, int], object]],
) -> None:
    input_dir, _arrays = tile_grid_2x2
    paths = scan_tiles(input_dir)

    sequential = load_tile_set(paths, max_workers=1)
    parallel = load_tile_set(paths, max_workers=8)

    assert sequential.crs == parallel.crs
    assert sequential.dtype == parallel.dtype
    assert sequential.resolution == parallel.resolution
    assert sequential.nodata == parallel.nodata
    assert {t.path for t in sequential.tiles} == {t.path for t in parallel.tiles}
    assert len(sequential.tiles) == len(parallel.tiles) == 4


def test_load_tile_set_default_workers_scale_with_tile_count(
    tile_grid_2x2: tuple[Path, dict[tuple[int, int], object]],
) -> None:
    input_dir, _arrays = tile_grid_2x2
    paths = scan_tiles(input_dir)
    # No explicit max_workers -- exercises _default_scan_workers().
    tile_set = load_tile_set(paths)
    assert len(tile_set.tiles) == 4


def test_fast_validate_still_catches_a_sampled_mismatch(tmp_path: Path) -> None:
    input_dir = tmp_path / "tiles"
    input_dir.mkdir()
    write_tile(input_dir / "a.tif", tx=0, ty=0, resolution=0.5)
    write_tile(input_dir / "b.tif", tx=1, ty=0, resolution=2.0)

    # Both tiles fall inside a full sample (n=2 <= sample_size), so
    # fast_validate must still catch the mismatch.
    with pytest.raises(TileGridError, match="pixel size"):
        load_tile_set(scan_tiles(input_dir), fast_validate=True, sample_size=200)


def test_fast_validate_skips_cross_checks_outside_the_sample(tmp_path: Path) -> None:
    input_dir = tmp_path / "tiles"
    input_dir.mkdir()
    # A reference tile plus several consistent ones, plus one mismatched tile
    # placed so a tiny sample_size (1, so only index 0 and the last index are
    # validated) skips over it.
    write_tile(input_dir / "a_ref.tif", tx=0, ty=0, resolution=0.5)
    write_tile(input_dir / "b_mismatch.tif", tx=1, ty=0, resolution=2.0)
    write_tile(input_dir / "c_ok.tif", tx=2, ty=0, resolution=0.5)
    write_tile(input_dir / "d_last.tif", tx=3, ty=0, resolution=0.5)

    paths = scan_tiles(input_dir)  # sorted: a_ref, b_mismatch, c_ok, d_last
    # sample_size=1 -> only index 0 and index len-1 (3) are validated,
    # skipping the mismatched tile at index 1.
    tile_set = load_tile_set(paths, fast_validate=True, sample_size=1)
    assert len(tile_set.tiles) == 4


def test_scan_tile_bounds_matches_load_tile_set_bounds(
    tile_grid_2x2: tuple[Path, dict[tuple[int, int], object]],
) -> None:
    input_dir, _arrays = tile_grid_2x2
    paths = scan_tiles(input_dir)

    bounds = scan_tile_bounds(paths)
    tile_set = load_tile_set(paths)

    assert sorted(bounds) == sorted(t.bounds for t in tile_set.tiles)
