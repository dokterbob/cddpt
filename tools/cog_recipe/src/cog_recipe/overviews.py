"""Explicit (non-power-of-2) overview factor computation.

GDAL's COG driver, left to its own devices, only ever builds power-of-2
overview levels. None of those lands exactly on a contour-friendly
resolution like 10 m for 0.5 m or 2 m native data. GDAL's overview *builder*
(``Dataset.build_overviews`` / ``gdaladdo``) has no such restriction -- it
accepts arbitrary integer decimation factors -- so this module computes an
explicit factor list that is guaranteed to include the exact factor for
``ensure_overview_res``, plus a power-of-2 pyramid around it.

**Why every level in the returned list is an exact power-of-2 multiple of
its predecessor** (verified empirically -- see ``tests/test_merge.py``):
when asked to build several overview levels in one call, GDAL's overview
generator does not recompute every level independently from the full-
resolution source; for speed, it cascades each new level from the nearest
*previously built* level. Averaging-then-averaging-again over an exact
integer ratio is mathematically identical to averaging directly from the
source (it's just associativity of the mean over a uniform partition) --
but cascading over a *non*-integer ratio (e.g. building a factor-5 level
from an existing factor-4 one, ratio 1.25) is a different, approximate
resampling, and was measured to introduce error an order of magnitude
larger than typical LERC quantization noise. So the power-of-2 levels
*below* the guaranteed factor are restricted to those that evenly divide
it -- for a prime or otherwise power-of-2-unfriendly factor (e.g. 2 m
native / 10 m guaranteed -> factor 5, which has no power-of-2 divisor other
than 1) this means no intermediate zoom level below the guaranteed one; a
deliberate, disclosed correctness-over-convenience trade-off.
"""

from __future__ import annotations

from cog_recipe.errors import TileGridError

_RATIO_TOLERANCE = 1e-6


def compute_overview_factors(
    native_resolution: float,
    ensure_overview_res: float,
    width: int,
    height: int,
    *,
    stop_dim: int = 512,
) -> list[int]:
    """Return a strictly increasing list of overview decimation factors.

    Guarantees:

    - a power-of-2 pyramid below the ``ensure_overview_res`` factor (for
      smooth zooming);
    - the exact integer factor that reaches ``ensure_overview_res`` (unless
      it already equals the native resolution, factor 1, which needs no
      extra level);
    - a power-of-2 continuation above it;
    - the list stops once the smallest overview's longest side is
      ``<= stop_dim`` pixels.

    Raises :class:`TileGridError` if ``ensure_overview_res`` is not an
    (approximately) integer multiple of ``native_resolution`` -- GDAL
    overview factors are integers, so no exact decimation reaches an
    in-between resolution.
    """

    if native_resolution <= 0 or ensure_overview_res <= 0:
        raise TileGridError("resolutions must be positive")
    if ensure_overview_res < native_resolution:
        raise TileGridError(
            f"--ensure-overview-res ({ensure_overview_res}) must be >= the native "
            f"resolution ({native_resolution}); it names an overview (coarser) level, "
            "not a resampling target."
        )

    ratio = ensure_overview_res / native_resolution
    factor = round(ratio)
    if factor < 1 or abs(ratio - factor) > _RATIO_TOLERANCE * max(1.0, factor):
        raise TileGridError(
            f"--ensure-overview-res={ensure_overview_res} is not an integer multiple of "
            f"the native resolution ({native_resolution}); ratio={ratio:.6f}. GDAL "
            "overview factors must be integers, so no exact decimation reaches that "
            f"resolution. Choose a multiple of {native_resolution} instead, or omit "
            "--ensure-overview-res to skip the guarantee."
        )

    max_dim = max(width, height)
    factors: list[int] = []

    if factor == 1:
        # The native resolution already *is* the requested overview
        # resolution -- no extra level needed, just a standard pyramid.
        f = 2
        while True:
            factors.append(f)
            if max_dim // f <= stop_dim:
                break
            f *= 2
        return factors

    f = 2
    while f < factor:
        if factor % f == 0:
            factors.append(f)
        f *= 2
    factors.append(factor)

    if max_dim // factor > stop_dim:
        f = factor * 2
        while True:
            factors.append(f)
            if max_dim // f <= stop_dim:
                break
            f *= 2

    return factors
