from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rio_cogeo.cogeo import cog_validate

from cog_recipe.atomic import cleanup_stray_tmp
from cog_recipe.convert import convert_directory, convert_tile
from cog_recipe.errors import ValidationError
from cog_recipe.validate import validate_cog

MAX_Z_ERROR = 0.05
EPS = 1e-4


def test_convert_tile_produces_valid_cog(single_tile: tuple[Path, Path, np.ndarray]) -> None:
    input_dir, source_path, source_array = single_tile
    dest = input_dir.parent / "out" / source_path.name
    dest.parent.mkdir()

    result = convert_tile(source_path, dest, max_z_error=MAX_Z_ERROR)

    assert result.ok
    assert not result.skipped
    assert dest.exists()

    is_valid, errors, _ = cog_validate(str(dest))
    assert is_valid, errors

    with rasterio.open(source_path) as src, rasterio.open(dest) as out:
        assert out.width == src.width
        assert out.height == src.height
        assert out.crs == src.crs
        assert out.nodata == src.nodata
        assert out.dtypes[0] == src.dtypes[0] == "float32"

        out_data = out.read(1)
        valid = source_array != src.nodata
        diff = np.abs(out_data[valid] - source_array[valid])
        assert diff.max() <= MAX_Z_ERROR + EPS


def test_convert_tile_resume_skip(single_tile: tuple[Path, Path, np.ndarray]) -> None:
    input_dir, source_path, _ = single_tile
    dest = input_dir.parent / "out" / source_path.name
    dest.parent.mkdir()

    first = convert_tile(source_path, dest, resume=True)
    assert first.ok and not first.skipped
    mtime_before = dest.stat().st_mtime_ns

    second = convert_tile(source_path, dest, resume=True)
    assert second.ok and second.skipped
    assert dest.stat().st_mtime_ns == mtime_before  # untouched, not rewritten


def test_convert_tile_force_overwrites(single_tile: tuple[Path, Path, np.ndarray]) -> None:
    input_dir, source_path, _ = single_tile
    dest = input_dir.parent / "out" / source_path.name
    dest.parent.mkdir()

    convert_tile(source_path, dest, resume=True)
    result = convert_tile(source_path, dest, resume=False)
    assert result.ok and not result.skipped


def test_stray_tmp_cleanup(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    stray = output_dir / "leftover.tif.tmp"
    stray.write_bytes(b"garbage from an interrupted previous run")

    removed = cleanup_stray_tmp(output_dir)

    assert stray in removed
    assert not stray.exists()


def test_atomic_rename_on_validation_failure(
    single_tile: tuple[Path, Path, np.ndarray], monkeypatch: pytest.MonkeyPatch
) -> None:
    input_dir, source_path, _ = single_tile
    dest = input_dir.parent / "out" / source_path.name
    dest.parent.mkdir()

    import cog_recipe.convert as convert_mod

    def _always_fail(*args: object, **kwargs: object) -> None:
        raise ValidationError("deliberately failing validation for this test")

    monkeypatch.setattr(convert_mod, "validate_cog", _always_fail)

    result = convert_tile(source_path, dest, resume=True)

    assert not result.ok
    assert "deliberately failing" in result.error
    assert not dest.exists()
    assert not list(dest.parent.glob("*.tmp"))


def test_validate_cog_catches_crs_and_nodata_mismatch(
    single_tile: tuple[Path, Path, np.ndarray],
) -> None:
    input_dir, source_path, _ = single_tile
    dest = input_dir.parent / "out" / source_path.name
    dest.parent.mkdir()
    convert_tile(source_path, dest)

    with rasterio.open(dest) as ds:
        real_crs = ds.crs
        real_nodata = ds.nodata

    with pytest.raises(ValidationError, match="CRS mismatch"):
        validate_cog(
            dest,
            expected_crs=rasterio.crs.CRS.from_epsg(4326),
            expected_nodata=real_nodata,
            expected_dtype="float32",
        )

    with pytest.raises(ValidationError, match="nodata mismatch"):
        validate_cog(
            dest,
            expected_crs=real_crs,
            expected_nodata=-1234.0,
            expected_dtype="float32",
        )


def test_convert_directory_writes_failed_csv_and_summary(
    tile_grid_2x2: tuple[Path, dict[tuple[int, int], np.ndarray]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir, _arrays = tile_grid_2x2
    output_dir = input_dir.parent / "out"

    import cog_recipe.convert as convert_mod

    calls = {"n": 0}
    real_convert_tile = convert_mod.convert_tile

    def _flaky_convert_tile(source_path: Path, dest_path: Path, **kwargs: object):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            return convert_mod.TileResult(
                source_path, dest_path, ok=False, error="synthetic failure"
            )
        return real_convert_tile(source_path, dest_path, **kwargs)

    monkeypatch.setattr(convert_mod, "convert_tile", _flaky_convert_tile)

    summary = convert_directory(input_dir, output_dir)

    assert summary.failed == 1
    assert summary.processed == 3
    assert summary.failed_csv is not None
    assert summary.failed_csv.exists()
    rows = summary.failed_csv.read_text().splitlines()
    assert rows[0] == "source_path,error,timestamp"
    assert "synthetic failure" in rows[1]

    assert summary.summary_json is not None
    data = json.loads(summary.summary_json.read_text())
    assert data["processed"] == 3
    assert data["failed"] == 1
