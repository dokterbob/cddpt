from __future__ import annotations

from itertools import pairwise

import pytest

from cog_recipe.errors import TileGridError
from cog_recipe.overviews import compute_overview_factors


def test_factors_include_exact_ensure_res_factor() -> None:
    # native 0.5 m, ensure 10 m -> factor 20 must be present, resolutions
    # native*factor must include exactly 10.0.
    factors = compute_overview_factors(0.5, 10.0, width=100_000, height=40_000, stop_dim=512)
    assert 20 in factors
    assert factors == sorted(factors)
    assert len(factors) == len(set(factors))
    resolutions = [0.5 * f for f in factors]
    assert 10.0 in resolutions
    # power-of-2 levels below 20 are present, but only those that evenly
    # divide 20 (so every step in the cascade is an exact integer ratio --
    # see overviews.py's module docstring): 8 and 16 do not divide 20.
    assert {2, 4}.issubset(set(factors))
    assert 8 not in factors
    assert 16 not in factors
    for a, b in pairwise(factors):
        assert b % a == 0, f"{b} is not an exact multiple of {a}"
    # stops once smallest overview side <= stop_dim (512)
    max_dim = 100_000
    assert max_dim // factors[-1] <= 512
    assert max_dim // factors[-2] > 512 or len(factors) == 1


def test_factors_non_power_of_two_native() -> None:
    # 2 m native, ensure 10 m -> factor 5 (prime: no power-of-2 divisor
    # other than 1, so no intermediate zoom level below it -- see the
    # module docstring's disclosed correctness-over-convenience trade-off).
    factors = compute_overview_factors(2.0, 10.0, width=2000, height=2000, stop_dim=64)
    assert factors[0] == 5
    resolutions = [2.0 * f for f in factors]
    assert 10.0 in resolutions
    for a, b in pairwise(factors):
        assert b % a == 0


def test_factor_equal_to_native_needs_no_extra_level() -> None:
    factors = compute_overview_factors(10.0, 10.0, width=4096, height=4096, stop_dim=512)
    # no factor of 1 (meaningless); a normal power-of-2 pyramid instead.
    assert 1 not in factors
    assert factors[0] == 2


def test_non_integer_ratio_raises_clear_error() -> None:
    with pytest.raises(TileGridError, match="integer multiple"):
        compute_overview_factors(3.0, 10.0, width=1000, height=1000)


def test_ensure_res_below_native_raises() -> None:
    with pytest.raises(TileGridError, match=">="):
        compute_overview_factors(10.0, 5.0, width=1000, height=1000)


def test_factors_strictly_increasing_small_mosaic() -> None:
    # Small synthetic-test-sized mosaic (400x400), native 0.5, ensure 2.5 (factor 5).
    factors = compute_overview_factors(0.5, 2.5, width=400, height=400, stop_dim=100)
    assert factors == sorted(factors)
    assert 5 in factors
