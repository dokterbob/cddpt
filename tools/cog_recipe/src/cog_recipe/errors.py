"""Exception hierarchy for cog_recipe."""

from __future__ import annotations


class CogRecipeError(Exception):
    """Base class for all cog_recipe errors."""


class GdalCapabilityError(CogRecipeError):
    """Raised when the local GDAL build lacks a required capability.

    Chiefly LERC_ZSTD compression support -- checked once, up front, so a
    92,000-tile batch fails in under a second with one clear message instead
    of failing on tile 1 with a cryptic GDAL driver error, and again on
    every tile after it.
    """


class TileGridError(CogRecipeError):
    """Raised when input tiles don't form a consistent, mosaicable grid.

    E.g. mixed CRS, mixed pixel size, mixed dtype, or (for the overview
    machinery) a requested overview resolution that isn't an integer
    multiple of the native resolution.
    """


class ValidationError(CogRecipeError):
    """Raised when a written COG fails post-write validation.

    Gates the atomic rename: a `.tmp` file that fails validation is never
    renamed to its final path, so "exists at the final path" always implies
    "validated".
    """
