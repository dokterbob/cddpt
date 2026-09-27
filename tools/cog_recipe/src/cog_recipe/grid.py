"""Bounds/grid arithmetic shared by mosaic building and bbox clipping."""

from __future__ import annotations

import math

Bounds = tuple[float, float, float, float]  # (minx, miny, maxx, maxy)


def union_bounds(bounds_list: list[Bounds]) -> Bounds:
    """The smallest bounds enclosing every entry in ``bounds_list``."""

    if not bounds_list:
        raise ValueError("no bounds to union")
    minx = min(b[0] for b in bounds_list)
    miny = min(b[1] for b in bounds_list)
    maxx = max(b[2] for b in bounds_list)
    maxy = max(b[3] for b in bounds_list)
    return (minx, miny, maxx, maxy)


def snap_outward(bounds: Bounds, grid_res: float) -> Bounds:
    """Expand ``bounds`` outward to the nearest multiple of ``grid_res``.

    Used to snap a user-supplied ``--bbox`` clip to the overview grid (e.g.
    10 m) so that both the native pixel grid (an integer multiple of
    ``grid_res`` divides it, checked by :func:`compute_overview_factors`)
    and the guaranteed overview level stay pixel-aligned across independent
    ``build-cog merge`` invocations with different clips.
    """

    minx, miny, maxx, maxy = bounds
    return (
        math.floor(minx / grid_res) * grid_res,
        math.floor(miny / grid_res) * grid_res,
        math.ceil(maxx / grid_res) * grid_res,
        math.ceil(maxy / grid_res) * grid_res,
    )


def clamp(bounds: Bounds, limits: Bounds) -> Bounds:
    """Clamp ``bounds`` to lie within ``limits`` (no data exists outside)."""

    minx, miny, maxx, maxy = bounds
    lminx, lminy, lmaxx, lmaxy = limits
    clamped = (max(minx, lminx), max(miny, lminy), min(maxx, lmaxx), min(maxy, lmaxy))
    if clamped[0] >= clamped[2] or clamped[1] >= clamped[3]:
        raise ValueError(
            f"clipped bounds {bounds} do not overlap the available tile extent {limits}"
        )
    return clamped
