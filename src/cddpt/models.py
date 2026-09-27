"""Small, typed value objects returned by :mod:`cddpt.catalog` and
:mod:`cddpt.download`.

Milestone 2 (anonymous catalog browsing, AOI handling, search) added
``CollectionInfo``/``AssetRef``/``CollectionEstimate``/``SearchEstimate``.
Milestone 5 (the downloader) adds ``DownloadStatus``/``DownloadOutcome``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

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


class DownloadStatus(str, Enum):
    """The final disposition of one asset's download attempt
    (:mod:`cddpt.download`, Milestone 5)."""

    #: Downloaded from scratch (no pre-existing ``.part`` file).
    downloaded = "downloaded"
    #: Completed by resuming a pre-existing ``.part`` file via ``Range``.
    resumed = "resumed"
    #: The final file already existed with a matching size -- no network
    #: calls were made at all (see ``Downloader.plan``).
    skipped = "skipped"
    #: Failed in a way that could not be recovered within the retry budget
    #: (see ``download.py``'s module docstring). A ``.part`` file, if any,
    #: is deliberately left in place for a future resume.
    failed = "failed"


@dataclass(frozen=True, slots=True)
class DownloadOutcome:
    """The result of one asset's download attempt (Milestone 5).

    ``dest`` is the asset's final on-disk path (per whatever
    :class:`~cddpt.naming.Layout` was used) regardless of ``status`` -- even
    a ``failed`` outcome names where a completed download would have landed
    (and where its ``.part`` file, if any, was left for a resume).
    ``bytes_transferred`` is how many bytes are on disk right now: the whole
    file for ``downloaded``/``resumed``/``skipped``, or whatever a ``.part``
    holds for ``failed``.

    Deliberately carries **no** download token, pre-signed URL, or cookie
    value anywhere -- see docs/PLAN.md's secrets-handling rules.
    ``cddpt.download``'s manifest writer serializes this via an explicit
    field allowlist (never ``asset.href``, a single-use download token
    embedded in its own URL path -- see ``catalog.py``/``download.py``).
    """

    asset: AssetRef
    dest: Path
    status: DownloadStatus
    bytes_transferred: int
    storage_filename: str
    error: str | None = None


__all__ = [
    "AssetRef",
    "CollectionEstimate",
    "CollectionInfo",
    "DownloadOutcome",
    "DownloadStatus",
    "SearchEstimate",
]
