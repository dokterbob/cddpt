"""Small, typed value objects returned by :mod:`cddpt.catalog`.

Kept deliberately minimal for Milestone 2 (anonymous catalog browsing, AOI
handling, search). ``DownloadOutcome`` belongs to the downloader (Milestone
5) and is intentionally not defined here yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pystac
    from shapely.geometry.base import BaseGeometry

    from .tiles import TileKey


@dataclass(frozen=True, slots=True)
class CollectionInfo:
    """One entry from ``/collections`` (or ``/collections/{id}``), unwrapped
    from DGT's envelope and parsed as a lenient :class:`pystac.Collection`.

    ``license`` is read verbatim from the raw API payload -- see
    ``catalog.py``'s module docstring for why this must *not* be read via
    ``pystac.Collection.license`` (pystac silently rewrites the deprecated
    ``"proprietary"``/``"various"`` values to ``"other"`` during its default
    migration step). Per docs/PLAN.md, cddpt never asserts a license itself.

    ``visibility`` is the raw list found at ``summaries.visibility`` in the
    live payload (e.g. ``("show",)`` or ``("hide",)``) -- see
    :func:`cddpt.catalog.CddCatalog.downloadable_collection_ids`.

    ``has_data_asset`` is ``None`` for every collection returned by
    :meth:`~cddpt.catalog.CddCatalog.collections`: live payloads carry no
    ``item_assets`` (or equivalent) on the collection resource, so this
    cannot be determined without sampling actual items. See
    ``catalog.py``'s module docstring for the full finding.
    """

    id: str
    title: str | None
    description: str | None
    license: str
    visibility: tuple[str, ...]
    has_data_asset: bool | None
    #: The raw, lenient pystac.Collection this was parsed from (excluded from
    #: equality/repr: it is a mutable, non-comparable third-party object).
    collection: pystac.Collection = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class AssetRef:
    """One data-bearing asset of one search result item.

    ``geometry`` is always the item's real (WGS84) footprint -- see
    ``catalog.py`` for why ``item.bbox`` itself is never exposed here.
    """

    item_id: str
    collection_id: str
    asset_key: str
    href: str
    size_bytes: int | None
    media_type: str | None
    tile_key: TileKey | None
    geometry: BaseGeometry


@dataclass(frozen=True, slots=True)
class CollectionEstimate:
    """One collection's contribution to a :class:`SearchEstimate`."""

    collection_id: str
    item_count: int
    known_bytes: int
    unknown_size_count: int


@dataclass(frozen=True, slots=True)
class SearchEstimate:
    """Aggregate size/count estimate for a search, without downloading anything.

    ``unknown_size_count`` is tracked separately from ``total_known_bytes``
    rather than assuming 0 bytes for assets missing ``file:size`` (e.g.
    orthophotos) -- see docs/PLAN.md, "Key findings".
    """

    item_count: int
    total_known_bytes: int
    unknown_size_count: int
    per_collection: tuple[CollectionEstimate, ...] = field(default_factory=tuple)


__all__ = [
    "AssetRef",
    "CollectionEstimate",
    "CollectionInfo",
    "SearchEstimate",
]
