"""Split a tile set's extent into aligned regional bboxes.

For 0.5 m national-scale merges (mainland Portugal: ~1.12M x 0.44M px, ~490
Gpx), doing the whole country in one ``build-cog merge`` call is usually not
the right move -- a large peak-disk footprint and a single multi-hour run
with no incremental progress. This module computes a grid of ``--bbox``
values tiling the input tiles' union extent into roughly ``region_size_m``-ish
squares, each snapped to the ``grid_res`` (typically ``--ensure-overview-res``)
grid via :func:`cog_recipe.grid.snap_outward` -- the same grid ``merge``'s own
``--bbox`` clipping snaps to -- so every region's COG stays pixel- and
overview-aligned with its neighbours.

This does **not** merge regions back together; each region is an
independent ``build-cog merge`` invocation (parallelizable across
processes/machines/disks) producing its own seamless-within-itself COG. See
``build-cog plan-regions`` (``cli.py``) and docs/build-cog.md "National-scale
runs".
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from cog_recipe.grid import Bounds, snap_outward


@dataclass(frozen=True, slots=True)
class Region:
    """One tile of the region grid.

    ``index`` is ``(col, row)`` in the region grid (not a pixel/tile-id
    coordinate); ``bounds`` is the region's ``(minx, miny, maxx, maxy)``,
    already snapped to ``grid_res`` and clipped to the mosaic's own extent.
    """

    index: tuple[int, int]
    bounds: Bounds


def compute_region_grid(
    mosaic_bounds: Bounds,
    region_size_m: float,
    grid_res: float,
) -> list[Region]:
    """Tile ``mosaic_bounds`` into ``region_size_m``-ish squares on the ``grid_res`` grid.

    ``mosaic_bounds`` is first snapped outward to ``grid_res`` (as ``merge
    --bbox`` does), and ``region_size_m`` itself is snapped up to the
    nearest multiple of ``grid_res`` -- so every region boundary lands
    exactly on the overview grid. Regions are contiguous, non-overlapping,
    and laid out row-major from the mosaic's (minx, miny) corner; edge
    regions are clipped to the mosaic's own snapped bounds, so the last
    column/row may be smaller than a full square.
    """

    if region_size_m <= 0:
        raise ValueError("region_size_m must be positive")
    if grid_res <= 0:
        raise ValueError("grid_res must be positive")

    minx, miny, maxx, maxy = snap_outward(mosaic_bounds, grid_res)
    step = math.ceil(region_size_m / grid_res) * grid_res

    n_cols = max(1, math.ceil((maxx - minx) / step))
    n_rows = max(1, math.ceil((maxy - miny) / step))

    regions: list[Region] = []
    for row in range(n_rows):
        for col in range(n_cols):
            rminx = minx + col * step
            rminy = miny + row * step
            rmaxx = min(rminx + step, maxx)
            rmaxy = min(rminy + step, maxy)
            regions.append(Region(index=(col, row), bounds=(rminx, rminy, rmaxx, rmaxy)))
    return regions
