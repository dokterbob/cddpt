"""cddpt-build-cog: re-encode DGT CDD GeoTIFF tiles into a seamless COG.

A standalone downstream *usage* of `cddpt`'s output, not part of the core
package: given a directory of downloaded plain-GeoTIFF DTM/DSM tiles (e.g.
from ``cddpt download --collection MDT-50cm``), build a single seamless
Cloud-Optimized GeoTIFF, LERC_ZSTD-compressed, at native resolution, with a
guaranteed overview level at a chosen contour-friendly resolution (default
10 m).

Everything below the CLI layer (:mod:`cog_recipe.cli`) is plain, typed,
side-effect-free-where-possible Python built on `rasterio` (whose wheel
bundles its own GDAL -- no system GDAL / ``osgeo`` install required; see
``gdal_support.py``) plus `rio-cogeo` for the secondary per-tile path. The
core is intentionally free of ``typer``/``tqdm`` so it can later be folded
into ``cddpt`` itself (e.g. a ``cddpt build-cog`` subcommand) without a
redesign: pass a :class:`~cog_recipe.progress.ProgressCallback` in and get a
typed result dataclass back.
"""

from __future__ import annotations

from cog_recipe.convert import ConvertSummary, TileResult, convert_directory, convert_tile
from cog_recipe.errors import GdalCapabilityError, TileGridError, ValidationError
from cog_recipe.gdal_support import check_lerc_zstd_support
from cog_recipe.merge import MergeResult, merge_tiles
from cog_recipe.overviews import compute_overview_factors
from cog_recipe.progress import ProgressCallback, ProgressEvent
from cog_recipe.tileset import TileInfo, TileSet, load_tile_set, scan_tiles

__all__ = [
    "ConvertSummary",
    "GdalCapabilityError",
    "MergeResult",
    "ProgressCallback",
    "ProgressEvent",
    "TileGridError",
    "TileInfo",
    "TileResult",
    "TileSet",
    "ValidationError",
    "check_lerc_zstd_support",
    "compute_overview_factors",
    "convert_directory",
    "convert_tile",
    "load_tile_set",
    "merge_tiles",
    "scan_tiles",
]

__version__ = "0.1.0"
