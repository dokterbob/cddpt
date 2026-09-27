"""Post-write COG validation, gating the atomic rename."""

from __future__ import annotations

import math
from pathlib import Path

import rasterio
from rasterio.crs import CRS
from rio_cogeo.cogeo import cog_validate

from cog_recipe.errors import ValidationError


def validate_cog(
    path: Path,
    *,
    expected_crs: CRS,
    expected_nodata: float,
    expected_dtype: str,
    expected_size: tuple[int, int] | None = None,
    required_overview_resolutions: list[float] | None = None,
) -> None:
    """Validate a written COG, raising :class:`ValidationError` on any failure.

    Checks (see docs/build-cog.md "Verification"): `rio_cogeo.cog_validate`,
    then a width/height/CRS/nodata/dtype round-trip, then -- for the merge
    command -- that an overview exists at each resolution in
    ``required_overview_resolutions`` (e.g. the guaranteed 10 m level).
    """

    errors: list[str] = []

    is_valid, cog_errors, _warnings = cog_validate(str(path))
    if not is_valid:
        errors.append(f"cog_validate failed: {cog_errors}")

    with rasterio.open(path) as ds:
        if ds.crs != expected_crs:
            errors.append(f"CRS mismatch: {ds.crs} != {expected_crs}")
        if ds.nodata is None or not math.isclose(ds.nodata, expected_nodata, rel_tol=1e-9):
            errors.append(f"nodata mismatch: {ds.nodata} != {expected_nodata}")
        if ds.dtypes[0] != expected_dtype:
            errors.append(f"dtype mismatch: {ds.dtypes[0]} != {expected_dtype}")
        if expected_size is not None and (ds.width, ds.height) != expected_size:
            errors.append(f"size mismatch: {(ds.width, ds.height)} != {expected_size}")

        if required_overview_resolutions:
            actual_res = {round(ds.transform.a * f, 9) for f in ds.overviews(1)}
            for required in required_overview_resolutions:
                if not any(math.isclose(r, required, rel_tol=1e-6) for r in actual_res):
                    errors.append(
                        f"no overview at the required resolution {required} "
                        f"(available: {sorted(actual_res)})"
                    )

    if errors:
        raise ValidationError(f"{path}: " + "; ".join(errors))
