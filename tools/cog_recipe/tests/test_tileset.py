from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_tile

from cog_recipe.errors import TileGridError
from cog_recipe.tileset import DEFAULT_NODATA, load_tile_set, scan_tiles


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
