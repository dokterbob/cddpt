from __future__ import annotations

import pytest

from cog_recipe.regions import compute_region_grid


def test_region_grid_exact_tiling() -> None:
    # 200x200 mosaic, 50 m regions, grid res 2.5 (divides evenly) -> 4x4 = 16.
    regions = compute_region_grid((0.0, -200.0, 200.0, 0.0), region_size_m=50.0, grid_res=2.5)
    assert len(regions) == 16
    cols = {r.index[0] for r in regions}
    rows = {r.index[1] for r in regions}
    assert cols == {0, 1, 2, 3}
    assert rows == {0, 1, 2, 3}


def test_region_grid_bounds_are_contiguous_and_cover_the_mosaic() -> None:
    mosaic_bounds = (0.0, -200.0, 200.0, 0.0)
    regions = compute_region_grid(mosaic_bounds, region_size_m=50.0, grid_res=2.5)

    minx = min(r.bounds[0] for r in regions)
    miny = min(r.bounds[1] for r in regions)
    maxx = max(r.bounds[2] for r in regions)
    maxy = max(r.bounds[3] for r in regions)
    assert (minx, miny, maxx, maxy) == mosaic_bounds

    # No gaps: every region's bounds are on the grid.
    for r in regions:
        for value in r.bounds:
            assert value % 2.5 == pytest.approx(0.0, abs=1e-9)


def test_region_grid_edge_regions_are_clipped_not_overlapping() -> None:
    # 130 m extent, 50 m regions -> 3 columns, last one only 30 m wide.
    regions = compute_region_grid((0.0, 0.0, 130.0, 50.0), region_size_m=50.0, grid_res=10.0)
    cols = sorted({r.index[0] for r in regions})
    assert cols == [0, 1, 2]
    last_col_region = next(r for r in regions if r.index[0] == 2)
    assert last_col_region.bounds[2] - last_col_region.bounds[0] == pytest.approx(30.0)


def test_region_grid_size_snapped_up_to_grid_res() -> None:
    # region_size_m=17 with grid_res=10 should snap the step up to 20.
    regions = compute_region_grid((0.0, 0.0, 40.0, 20.0), region_size_m=17.0, grid_res=10.0)
    cols = {r.index[0] for r in regions}
    assert cols == {0, 1}  # 40 / 20 = 2 columns, not 40/17 -> 3


def test_region_grid_single_region_when_size_covers_mosaic() -> None:
    regions = compute_region_grid((0.0, 0.0, 30.0, 30.0), region_size_m=1000.0, grid_res=10.0)
    assert len(regions) == 1
    assert regions[0].bounds == (0.0, 0.0, 30.0, 30.0)


def test_region_grid_rejects_non_positive_inputs() -> None:
    with pytest.raises(ValueError):
        compute_region_grid((0.0, 0.0, 10.0, 10.0), region_size_m=0.0, grid_res=10.0)
    with pytest.raises(ValueError):
        compute_region_grid((0.0, 0.0, 10.0, 10.0), region_size_m=10.0, grid_res=0.0)
