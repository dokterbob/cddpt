"""Deterministic, path-traversal-safe on-disk layouts for downloaded assets.

Milestone 5's download design requires the destination path to be knowable
*before* the token -> pre-signed-URL exchange (docs/PLAN.md's M4 pre-work:
"The destination path must be deterministic BEFORE the exchange, so re-runs
can skip without spending tokens"). So every :class:`Layout` here derives a
filename purely from an :class:`~cddpt.models.AssetRef` -- never from the
real storage filename CDD's pre-signed URL happens to use internally (e.g.
``MDT-2m-111195-07-2024_v01.tif``, note the ``_v01`` suffix), which is only
known *after* spending a token and would not be stable/deterministic across
re-runs anyway. :func:`storage_filename` -- cddpt's *own* deterministic name
(item id + extension) -- is what every :class:`Layout` here actually uses on
disk, and is also what is recorded in the manifest (see ``download.py``'s
``DownloadOutcome.storage_filename``) -- never CDD's own internal filename,
which cddpt never learns (the pre-signed URL is followed as an opaque
transfer target, its path never parsed for a filename).

Extension mapping (derived by inspecting real, anonymous search results --
see docs/PLAN.md's Milestone 5 notes for the exact media types observed):

============================================================  =========
``asset.media_type`` (STAC ``type``, before any ``;`` params)  extension
============================================================  =========
``image/tiff`` (any ``;``-parameterised variant, e.g.          ``.tif``
``image/tiff; application=geotiff``,
``image/tiff; application=geotiff; profile=cloud-optimized``)
``application/vnd.laszip`` (``LAZ``)                           ``.laz``
============================================================  =========

Every other/unknown media type falls back to :mod:`mimetypes`' own guess,
then to a sane default (``.bin``) if even that fails -- never raises, since
an unrecognised media type must not block a download.
"""

from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .models import AssetRef
from .tiles import decode_tile_key

#: See this module's docstring: media types observed live (2026-09) against
#: real, anonymous search results for MDT-2m/MDS-2m/MDT-50cm/MDS-50cm (all
#: ``image/tiff; application=geotiff``), LAZ (``application/vnd.laszip``),
#: and an orthophoto COG (``image/tiff; application=geotiff;
#: profile=cloud-optimized``). Matched by prefix on the media type with any
#: ``;``-separated parameters stripped, so every one of those variants maps
#: to the same extension without needing to enumerate each parameter combo.
_MEDIA_TYPE_EXTENSIONS: tuple[tuple[str, str], ...] = (
    ("application/vnd.laszip", ".laz"),
    ("image/tiff", ".tif"),
)

#: Used when a media type is missing, unrecognised, and ``mimetypes`` itself
#: has no guess -- never raises, so an unrecognised media type never blocks
#: a download; the file is just named generically.
_DEFAULT_EXTENSION = ".bin"

#: Path separators (and NUL) that must never survive into a single path
#: *component* -- see :func:`_safe_component`.
_UNSAFE_CHARS = re.compile(r"[\\/\x00]")

#: Directory name used by :class:`ByTileLayout` for assets whose item id
#: does not decode as a CDD tile id (e.g. orthophotos).
DEFAULT_UNTILED_DIRNAME = "untiled"


def asset_extension(asset: AssetRef) -> str:
    """The filename extension (with leading dot) to use for ``asset``,
    derived from its STAC ``media_type`` -- never from a hardcoded
    per-collection table (mirrors ``catalog.py``'s role-driven asset
    selection: never hardcode what discovery/probing that revealed)."""

    if asset.media_type:
        base = asset.media_type.split(";", 1)[0].strip().lower()
        for prefix, extension in _MEDIA_TYPE_EXTENSIONS:
            if base == prefix or base.startswith(prefix + "/"):
                return extension
        guessed = mimetypes.guess_extension(base)
        if guessed:
            return guessed
    return _DEFAULT_EXTENSION


def _safe_component(value: str) -> str:
    """Make ``value`` safe to use as a single path *component* (never a
    whole path): strips/replaces path separators and NUL bytes so a
    server-supplied id can never smuggle in a ``../`` traversal or an
    absolute path, and strips leading dots so it can't collapse to ``"."``/
    ``".."`` or an unintentionally hidden file.

    Never raises and never produces an empty string (falls back to
    ``"_"``) -- a malformed/empty id must not crash a download, just land
    somewhere sane.
    """

    sanitized = _UNSAFE_CHARS.sub("_", value.strip())
    sanitized = sanitized.lstrip(".")
    return sanitized or "_"


def storage_filename(asset: AssetRef) -> str:
    """The filename cddpt uses on disk for ``asset``: its (sanitized) item
    id plus :func:`asset_extension` -- deterministic from the
    :class:`~cddpt.models.AssetRef` alone, before any token is ever minted.

    This is *not* necessarily the real filename CDD's pre-signed URL uses
    internally (e.g. ``MDT-2m-111195-07-2024_v01.tif``) -- that is only
    knowable after spending a token, which the destination path must not
    depend on. See this module's docstring.
    """

    return f"{_safe_component(asset.item_id)}{asset_extension(asset)}"


class Layout(Protocol):
    """How :class:`~cddpt.download.Downloader` maps one asset to a
    destination path under an ``--out`` directory.

    Every implementation must be a pure function of ``asset``/``out_dir``
    (no I/O, no randomness) so :meth:`~cddpt.download.Downloader.plan` can
    compute the exact same path on every run -- this is what lets a re-run
    skip an already-complete file without spending a download token.
    """

    def dest_for(self, asset: AssetRef, out_dir: Path) -> Path:
        """The destination file path for ``asset`` under ``out_dir``."""
        ...


@dataclass(frozen=True, slots=True)
class ByCollectionLayout:
    """The default layout: ``<out>/<collection_id>/<storage_filename>``."""

    def dest_for(self, asset: AssetRef, out_dir: Path) -> Path:
        return out_dir / _safe_component(asset.collection_id) / storage_filename(asset)


@dataclass(frozen=True, slots=True)
class ByTileLayout:
    """Groups by CDD tile grid cell: ``<out>/<tileX><tileY>/<storage_filename>``
    (e.g. ``111195`` for tile ``(111, 195)``), using
    :func:`cddpt.tiles.decode_tile_key`.

    Assets whose item id doesn't decode as a CDD tile id (e.g. orthophotos --
    see ``tiles.py``) land under :attr:`fallback_dirname` instead of being
    dropped or raising.
    """

    fallback_dirname: str = DEFAULT_UNTILED_DIRNAME

    def dest_for(self, asset: AssetRef, out_dir: Path) -> Path:
        tile_key = asset.tile_key if asset.tile_key is not None else decode_tile_key(asset.item_id)
        if tile_key is None:
            subdir = self.fallback_dirname
        else:
            subdir = f"{tile_key.tile_x:03d}{tile_key.tile_y:03d}"
        return out_dir / _safe_component(subdir) / storage_filename(asset)


@dataclass(frozen=True, slots=True)
class FlatLayout:
    """No subdirectories at all: ``<out>/<storage_filename>``.

    Note this can collide if two assets from different collections happen
    to share an item id and media type -- callers who expect that should
    prefer :class:`ByCollectionLayout` or :class:`ByTileLayout` instead.
    """

    def dest_for(self, asset: AssetRef, out_dir: Path) -> Path:
        return out_dir / storage_filename(asset)


__all__ = [
    "DEFAULT_UNTILED_DIRNAME",
    "ByCollectionLayout",
    "ByTileLayout",
    "FlatLayout",
    "Layout",
    "asset_extension",
    "storage_filename",
]
