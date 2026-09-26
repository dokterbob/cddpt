"""CDD catalog access: collection discovery, AOI/collection search, and asset
selection -- fully anonymous (Milestone 2: no auth, no downloading).

Envelope unwrapping
--------------------
``GET /collections`` and ``GET /collections/{id}`` are wrapped in DGT's own
envelope, ``{"status": 200, "message": "OK", "data": {...}}``. Live probing
(2026-09) found ``data`` for the list endpoint is itself *not* a bare STAC
``Collections`` object -- it's ``{"collections": [...], "models": ...,
"reservedKeys": ..., "keywords": ..., "translations": ..., "catalog": ...}``,
of which only ``"collections"`` (a plain list of STAC Collection dicts) is
used here. The single-collection endpoint's ``data`` *is* the bare STAC
Collection dict. ``POST /search`` (via :mod:`pystac_client`) is not
envelope-wrapped at all -- a bare, spec-shaped STAC ItemCollection page, per
docs/PLAN.md.

Where collection "visibility" actually lives
----------------------------------------------
Neither payload has a top-level ``visibility`` field. It lives at
``summaries.visibility`` as a list, e.g. ``["show"]`` or ``["hide"]``. Of the
21 live collections (2026-09), the 14 mainland ("continente") collections
all have ``visibility: ["show"]``; the 7 Azores ("acores") collections all
have ``visibility: ["hide"]``. ``summaries.access`` is ``["private"]`` for
*every* collection regardless of visibility -- it does not discriminate
downloadable from hidden and is not used here.

No collection payload (list or detail) carries an ``item_assets`` field or
any equivalent -- so :meth:`CddCatalog.downloadable_collection_ids` can only
use the ``visibility`` criterion; whether a collection actually has a
data-bearing asset can only be observed on real search results (see
:func:`_select_data_asset` below), not from collection-level metadata alone.
This is a deliberate, evidence-based deviation from the original spec
wording ("plus a data-role asset present in item_assets or equivalent").

``pystac``'s license migration silently mutates "proprietary"
-----------------------------------------------------------------
Every live collection declares ``"license": "proprietary"``. STAC deprecated
that value (along with ``"various"``) in favour of ``"other"``, and
``pystac.Collection.from_dict()`` rewrites it unconditionally during its
default migration step (``pystac.serialization.migrate._migrate_license``)
-- *regardless* of the input's declared ``stac_version`` (verified: even a
same-version-already dict gets rewritten). docs/PLAN.md is explicit that
cddpt must "never assert a license ourselves -- surface ``collection.license``
verbatim". So this module always calls ``pystac.Collection.from_dict(d,
migrate=False)`` and reads ``license`` from the *raw* dict, never from the
parsed pystac object's ``.license`` property.

The corrupted raster ``bbox`` is inconsistent, not collection-wide
-----------------------------------------------------------------------
Live probing found the corrupted-bbox behaviour described in docs/PLAN.md
(``item.bbox`` actually being a serialized GDAL geotransform,
``[originX, pixelWidth, rowRotation, originY, colRotation, pixelHeight]``)
is *not* a fixed property of a collection: e.g. ``MDT-2m`` items from an
older (2024) processing batch have the corrupted 6-element geotransform
bbox, while ``MDT-2m`` items from a newer (2025) batch in a different region
have an ordinary 4-element WGS84 bbox -- in the *same* collection. This
means no static "these collections are corrupted" allowlist would be safe;
:func:`_sanitize_item_bbox` therefore overwrites **every** item's ``bbox``
unconditionally, for every collection, with the bounds of its (always
correct) ``geometry``. This is the *only* place in ``src/`` that reads or
writes ``item.bbox``.

Asset role layout (live, 2026-09)
----------------------------------
- LiDAR-derived rasters (``MDT-*``, ``MDS-*``): single asset key
  ``"data"``, ``roles: ["data", "visual"]``.
- ``LAZ``: single asset key ``"data"``, ``roles: ["data"]``.
- Orthophotos (``ORTOS-*``): single asset key ``"visual"``, ``roles:
  ["visual"]`` -- **no asset has role "data" at all**. This directly
  contradicts docs/PLAN.md's assumption that every downloadable item has a
  ``"data"``-role asset; :func:`_select_data_asset` therefore prefers a
  ``"data"``-role asset but falls back to a ``"visual"``-role one. Asset
  selection is still entirely role-driven (never a hardcoded asset key).
- ``properties["file:size"]`` is present for LiDAR-derived products but
  absent for orthophotos, confirming docs/PLAN.md's note -- callers must
  track "unknown size" counts separately (see :class:`~cddpt.models.SearchEstimate`).

pystac-client session injection
---------------------------------
``pystac_client.stac_api_io.StacApiIO.__init__`` always creates its own bare
``requests.Session()`` and there is no constructor parameter to inject one.
The mechanism this module uses instead: construct a ``StacApiIO()`` (its
default session is never used for a real request) and immediately replace
its ``.session`` attribute with cddpt's own governed session (built via
:func:`cddpt.http.make_session`) before calling anything on it; then pass
that ``StacApiIO`` as ``ItemSearch(..., stac_io=stac_io)``. This was
confirmed against the live API standalone (``pystac_client.Client.open()``
cannot be used at all -- root/``/conformance``/``/queryables`` 302 to
login).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any

import pystac
import requests
from pystac_client.item_search import DatetimeLike, ItemSearch
from pystac_client.stac_api_io import StacApiIO
from shapely.geometry import shape

from .aoi import Aoi
from .errors import ApiError, ConfigError
from .http import make_session
from .models import AssetRef, CollectionEstimate, CollectionInfo, SearchEstimate
from .ratelimit import RequestGovernor
from .settings import Settings
from .tiles import TileKey, decode_tile_key

#: A reasonable default page size for /search -- see docs/PLAN.md: the
#: server's own hard cap is 10,000 items/page.
DEFAULT_PAGE_SIZE = 1000
_MAX_PAGE_SIZE = 10000

#: Preference order for selecting a search result item's data-bearing
#: asset -- see this module's docstring, "Asset role layout".
_DATA_ASSET_ROLES = ("data", "visual")


def _parse_envelope(response: requests.Response, url: str) -> Any:
    """Unwrap DGT's ``{"status", "message", "data"}`` envelope.

    Raises :class:`~cddpt.errors.ApiError` for anything that doesn't match
    that shape, or whose inner ``status`` isn't 200.
    """

    if response.status_code != 200:
        raise ApiError(f"cddpt: unexpected HTTP status {response.status_code} from {url}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ApiError(f"cddpt: non-JSON response from {url}") from exc

    if not isinstance(payload, dict) or "data" not in payload:
        raise ApiError(f"cddpt: malformed envelope from {url}: missing 'data' ({payload!r})")

    status = payload.get("status")
    if status != 200:
        message = payload.get("message")
        raise ApiError(
            f"cddpt: API returned a non-200 envelope status {status!r} from {url} "
            f"(message: {message!r})"
        )

    return payload["data"]


def _collection_info_from_dict(d: Mapping[str, Any]) -> CollectionInfo:
    raw = dict(d)
    try:
        collection = pystac.Collection.from_dict(raw, migrate=False)
    except Exception as exc:  # pystac raises a variety of exception types
        raise ApiError(f"cddpt: could not parse collection {raw.get('id')!r}: {exc}") from exc

    visibility_raw = collection.summaries.get_list("visibility") if collection.summaries else None
    visibility = tuple(visibility_raw) if visibility_raw else ()

    return CollectionInfo(
        id=collection.id,
        title=collection.title,
        description=collection.description,
        # Verbatim from the raw payload -- never collection.license, which
        # pystac's migration step would have rewritten. See module docstring.
        license=str(raw.get("license", "")),
        visibility=visibility,
        has_data_asset=None,
        collection=collection,
    )


def _select_data_asset(item: pystac.Item) -> tuple[str, pystac.Asset] | None:
    """Pick the item's data-bearing asset, by ``roles`` only -- never a
    hardcoded asset key. See this module's docstring, "Asset role layout"."""

    for role in _DATA_ASSET_ROLES:
        for key, asset in item.assets.items():
            if asset.roles and role in asset.roles:
                return key, asset
    return None


def _sanitize_item_bbox(item: pystac.Item) -> None:
    """Overwrite ``item.bbox`` with the bounds of ``item.geometry``.

    This is the **only** place in ``src/`` that reads or writes
    ``pystac.Item.bbox`` -- see this module's docstring for why the
    corruption cannot be predicted from the collection alone, and must
    therefore be unconditionally corrected for every item. The original
    (possibly corrupted, possibly fine) bbox is preserved under
    ``properties["cddpt:raw_bbox"]`` since it was useful for tile-origin
    verification (see ``tests/test_tiles.py``).
    """

    raw_bbox = item.bbox
    if raw_bbox is not None:
        item.properties["cddpt:raw_bbox"] = list(raw_bbox)
    if item.geometry is not None:
        minx, miny, maxx, maxy = shape(item.geometry).bounds
        item.bbox = [minx, miny, maxx, maxy]
    else:
        item.bbox = None


class CddCatalog:
    """Anonymous access to CDD's collection catalog and item search.

    Parameters
    ----------
    settings:
        Defaults to a fresh :class:`~cddpt.settings.Settings` if omitted.
    session:
        An already-built governed session (e.g. shared with a future
        downloader). If omitted, one is built via
        :func:`cddpt.http.make_session`.
    governor:
        The shared :class:`~cddpt.ratelimit.RequestGovernor` for this run.
        Only used when ``session`` is not supplied (to build one); ignored
        otherwise. Defaults to a fresh governor built from ``settings``.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        session: requests.Session | None = None,
        governor: RequestGovernor | None = None,
    ) -> None:
        self._settings = settings if settings is not None else Settings()
        self._governor = (
            governor if governor is not None else RequestGovernor.from_settings(self._settings)
        )
        self._session = (
            session
            if session is not None
            else make_session(self._settings, governor=self._governor)
        )

    @property
    def search_url(self) -> str:
        return f"{self._settings.api_base_url}/search"

    def collections(self) -> list[CollectionInfo]:
        """All collections from ``GET /collections``, discovered at runtime
        (never hardcoded -- see docs/PLAN.md)."""

        url = f"{self._settings.api_base_url}/collections"
        response = self._session.get(url)
        payload = _parse_envelope(response, url)

        if not isinstance(payload, dict) or "collections" not in payload:
            raise ApiError(
                f"cddpt: malformed /collections envelope: missing 'data.collections' ({url})"
            )
        raw_collections = payload["collections"]
        if not isinstance(raw_collections, list):
            raise ApiError(f"cddpt: /collections 'data.collections' is not a list ({url})")

        return [_collection_info_from_dict(c) for c in raw_collections]

    def collection(self, collection_id: str) -> CollectionInfo:
        """One collection from ``GET /collections/{collection_id}``."""

        url = f"{self._settings.api_base_url}/collections/{collection_id}"
        response = self._session.get(url)
        payload = _parse_envelope(response, url)

        if not isinstance(payload, dict) or "id" not in payload:
            raise ApiError(f"cddpt: malformed /collections/{collection_id} envelope ({url})")

        return _collection_info_from_dict(payload)

    def downloadable_collection_ids(self) -> list[str]:
        """IDs of collections whose ``summaries.visibility`` contains
        ``"show"`` -- see this module's docstring for what "visibility"
        actually means in this API (and what it does *not* verify)."""

        return [c.id for c in self.collections() if "show" in c.visibility]

    def _build_item_search(
        self,
        *,
        aoi: Aoi,
        collections: Iterable[str],
        datetime: DatetimeLike | None,
        page_size: int,
    ) -> ItemSearch:
        stac_io = StacApiIO()
        # Discard StacApiIO's own bare Session -- never used for a real
        # request -- and use cddpt's single governed session instead. See
        # this module's docstring, "pystac-client session injection".
        stac_io.session = self._session
        return ItemSearch(
            url=self.search_url,
            method="POST",
            stac_io=stac_io,
            collections=list(collections),
            intersects=aoi.geometry,
            datetime=datetime,
            limit=page_size,
        )

    def iter_items(
        self,
        aoi: Aoi,
        collections: Iterable[str],
        *,
        datetime: DatetimeLike | None = None,
        chunk_km2: float | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Iterator[pystac.Item]:
        """Stream sanitized :class:`pystac.Item`\\ s matching ``aoi`` and
        ``collections``, chunking the AOI and de-duplicating across chunk
        boundaries by item ID. Never materializes the whole result set.

        Raises :class:`~cddpt.errors.ConfigError` if ``collections`` is
        empty -- every search must pass an explicit collection list (see
        docs/PLAN.md: "~21+ collections ... must be discovered from
        /collections at runtime, never hardcoded").
        """

        collection_ids = list(collections)
        if not collection_ids:
            raise ConfigError("cddpt: iter_items() requires at least one explicit collection id")
        if page_size < 1 or page_size > _MAX_PAGE_SIZE:
            raise ConfigError(
                f"cddpt: page_size must be between 1 and {_MAX_PAGE_SIZE} (got {page_size})"
            )

        effective_chunk_km2 = chunk_km2 if chunk_km2 is not None else self._settings.chunk_km2
        # Keyed by (collection, id): item ids are not guaranteed unique
        # across collections.
        seen_ids: set[tuple[str | None, str]] = set()

        for chunk in aoi.chunks(effective_chunk_km2):
            search = self._build_item_search(
                aoi=chunk,
                collections=collection_ids,
                datetime=datetime,
                page_size=page_size,
            )
            for item in search.items():
                key = (item.collection_id, item.id)
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                _sanitize_item_bbox(item)
                yield item

    def iter_assets(
        self,
        aoi: Aoi,
        collections: Iterable[str],
        *,
        datetime: DatetimeLike | None = None,
        chunk_km2: float | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Iterator[AssetRef]:
        """Like :meth:`iter_items`, but yields one :class:`~cddpt.models.AssetRef`
        per item for its data-bearing asset (see :func:`_select_data_asset`).
        Items with no data-bearing asset at all are skipped."""

        for item in self.iter_items(
            aoi,
            collections,
            datetime=datetime,
            chunk_km2=chunk_km2,
            page_size=page_size,
        ):
            selected = _select_data_asset(item)
            if selected is None:
                continue
            asset_key, asset = selected

            tile_key: TileKey | None = decode_tile_key(item.id)
            size_bytes = item.properties.get("file:size")
            if size_bytes is not None and not isinstance(size_bytes, int):
                size_bytes = int(size_bytes)

            assert item.geometry is not None  # sanitized in iter_items
            yield AssetRef(
                item_id=item.id,
                collection_id=item.collection_id or "",
                asset_key=asset_key,
                href=asset.href,
                size_bytes=size_bytes,
                media_type=asset.media_type,
                tile_key=tile_key,
                geometry=shape(item.geometry),
            )

    def estimate(
        self,
        aoi: Aoi,
        collections: Iterable[str],
        *,
        datetime: DatetimeLike | None = None,
        chunk_km2: float | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> SearchEstimate:
        """Count and size a search without downloading anything.

        Assets with no known ``file:size`` are counted separately (never
        assumed to be 0 bytes) -- see docs/PLAN.md's note on orthophotos.
        """

        item_count = 0
        total_known_bytes = 0
        unknown_size_count = 0
        per_collection: dict[str, list[int]] = {}  # id -> [count, known_bytes, unknown_count]

        for asset in self.iter_assets(
            aoi,
            collections,
            datetime=datetime,
            chunk_km2=chunk_km2,
            page_size=page_size,
        ):
            item_count += 1
            bucket = per_collection.setdefault(asset.collection_id, [0, 0, 0])
            bucket[0] += 1
            if asset.size_bytes is not None:
                total_known_bytes += asset.size_bytes
                bucket[1] += asset.size_bytes
            else:
                unknown_size_count += 1
                bucket[2] += 1

        per_collection_tuple = tuple(
            CollectionEstimate(
                collection_id=collection_id,
                item_count=counts[0],
                known_bytes=counts[1],
                unknown_size_count=counts[2],
            )
            for collection_id, counts in per_collection.items()
        )

        return SearchEstimate(
            item_count=item_count,
            total_known_bytes=total_known_bytes,
            unknown_size_count=unknown_size_count,
            per_collection=per_collection_tuple,
        )


__all__ = ["DEFAULT_PAGE_SIZE", "CddCatalog"]
