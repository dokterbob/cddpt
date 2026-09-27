"""Preflight check that the active GDAL build supports LERC_ZSTD.

Design note (see README "GDAL approach"): this tool deliberately never
imports ``osgeo`` and never shells out to system GDAL utilities
(``gdalbuildvrt``, ``gdalwarp``, ``gdal_translate``). Every raster operation
goes through `rasterio`, whose PyPI wheels vendor their own libgdal build.
That build's compression driver support is checked here, once, by actually
writing a tiny in-memory GeoTIFF with ``COMPRESS=LERC_ZSTD`` -- the only
reliable way to know, since GDAL's LERC and ZSTD support are both optional
build-time features and rasterio does not expose a "supported codecs" list.
"""

from __future__ import annotations

import numpy as np
import rasterio
from rasterio.io import MemoryFile

from cog_recipe.errors import GdalCapabilityError

_PROBE_SIZE = 2


def check_lerc_zstd_support() -> None:
    """Raise :class:`GdalCapabilityError` if LERC_ZSTD can't be written.

    Fails fast with one clear message instead of failing partway through a
    batch of tens of thousands of tiles.
    """

    data = np.zeros((_PROBE_SIZE, _PROBE_SIZE), dtype="float32")
    try:
        with (
            MemoryFile() as memfile,
            memfile.open(
                driver="GTiff",
                width=_PROBE_SIZE,
                height=_PROBE_SIZE,
                count=1,
                dtype="float32",
                compress="LERC_ZSTD",
            ) as dst,
        ):
            dst.write(data, 1)
    except Exception as exc:
        raise GdalCapabilityError(
            "The active GDAL build (via rasterio, version "
            f"{rasterio.__gdal_version__}) does not support LERC_ZSTD "
            "compression. cddpt-build-cog relies on rasterio's bundled "
            "GDAL and never falls back to system GDAL; reinstalling "
            "rasterio from a current PyPI wheel "
            "(`uv pip install --reinstall rasterio` / `uv sync --upgrade`) "
            "should provide a build with LERC and ZSTD support. Original "
            f"error: {exc}"
        ) from exc
