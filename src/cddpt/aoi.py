"""Area-of-interest handling: construction, reprojection, and chunking.

An :class:`Aoi` always holds its geometry in EPSG:4326 (WGS84, lon/lat
order, matching GeoJSON and shapely's ``__geo_interface__`` convention) --
every constructor reprojects into that CRS before returning. Downstream code
(``catalog.py``) can therefore always pass an ``Aoi``'s geometry straight to
``pystac_client.ItemSearch(intersects=...)`` without worrying about CRS.

Reprojection uses ``pyproj`` throughout with ``always_xy=True`` (so
coordinate order is always ``(x, y)`` / ``(lon, lat)``, never the
geographic-axis-order ``(lat, lon)`` that some CRS definitions default to).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping as ABCMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import shapely
from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box, shape
from shapely.geometry import mapping as _shapely_mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as _shapely_transform

from .errors import ConfigError

#: Every Aoi's geometry is stored in this CRS.
_WGS84 = CRS.from_epsg(4326)
#: The CRS CDD's own tile grid (and hence chunking) is defined in -- see
#: tiles.py. Also used for area_km2() whenever the AOI lies within its
#: declared area of use (mainland Portugal); see _metric_crs_for_bounds.
_PORTUGAL_TM06 = CRS.from_epsg(3763)
#: Equal-area fallback for AOIs outside mainland Portugal (e.g. the Azores,
#: or a user AOI anywhere else on Earth) -- World Cylindrical Equal Area,
#: valid everywhere between 86°S and 86°N.
_WORLD_EQUAL_AREA = CRS.from_epsg(6933)
#: See Aoi.chunks(): the smallest grid-intersection piece kept as a real
#: chunk (1 m² -- utterly insignificant next to a 1 km² tile).
_MIN_CHUNK_AREA_M2 = 1.0


def _reproject(geom: BaseGeometry, src: CRS, dst: CRS) -> BaseGeometry:
    transformer = Transformer.from_crs(src, dst, always_xy=True)
    return _shapely_transform(transformer.transform, geom)


def _valid(geom: BaseGeometry) -> BaseGeometry:
    """``shapely.make_valid`` -- every Aoi geometry passes through this."""

    return shapely.make_valid(geom)


def _polygonal_only(geom: BaseGeometry) -> BaseGeometry:
    """Drop stray point/line parts from a (possibly mixed) intersection
    result, keeping only polygonal area. Used by :meth:`Aoi.chunks`, where a
    grid cell can touch the AOI boundary at a single point or along an edge."""

    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    if isinstance(geom, GeometryCollection):
        polys = [g for g in geom.geoms if isinstance(g, (Polygon, MultiPolygon)) and not g.is_empty]
        if not polys:
            return Polygon()
        return shapely.union_all(polys) if len(polys) > 1 else polys[0]
    return Polygon()


def _metric_crs_for_bounds(bounds: tuple[float, float, float, float]) -> CRS:
    """EPSG:3763 if ``bounds`` (a WGS84 ``(minx, miny, maxx, maxy)``) lies
    within its declared area of use (mainland Portugal), else a worldwide
    equal-area CRS. See docs/PLAN.md: "computed in EPSG:3763 -- or an
    equal-area CRS if AOI lies outside mainland"."""

    minx, miny, maxx, maxy = bounds
    aou = _PORTUGAL_TM06.area_of_use
    assert aou is not None
    west, south, east, north = aou.bounds
    if west <= minx and south <= miny and maxx <= east and maxy <= north:
        return _PORTUGAL_TM06
    return _WORLD_EQUAL_AREA


def _crs_from_legacy_geojson(crs_member: Any) -> CRS:
    """Parse a legacy top-level GeoJSON ``"crs"`` member, e.g.::

        {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3763"}}

    This member was removed from the GeoJSON spec (RFC 7946) but is still
    produced by some GIS tools/exports. Raises :class:`ConfigError` if it is
    missing the expected shape or pyproj cannot parse the name.
    """

    if not isinstance(crs_member, ABCMapping):
        raise ConfigError(f"cddpt: unrecognised GeoJSON 'crs' member: {crs_member!r}")
    name = crs_member.get("properties", {}).get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError(f"cddpt: GeoJSON 'crs' member has no usable name: {crs_member!r}")
    try:
        return CRS(name)
    except CRSError as exc:
        raise ConfigError(f"cddpt: could not parse GeoJSON 'crs' name {name!r}: {exc}") from exc


def _crs_from_string(crs: str) -> CRS:
    try:
        return CRS(crs)
    except CRSError as exc:
        raise ConfigError(f"cddpt: could not parse CRS {crs!r}: {exc}") from exc


def _extract_geometries(data: ABCMapping[str, Any]) -> list[BaseGeometry]:
    obj_type = data.get("type")
    if obj_type == "FeatureCollection":
        geoms = []
        for feature in data.get("features", []):
            geom_dict = feature.get("geometry")
            if geom_dict is not None:
                geoms.append(shape(geom_dict))
        return geoms
    if obj_type == "Feature":
        geom_dict = data.get("geometry")
        return [shape(geom_dict)] if geom_dict is not None else []
    if obj_type is None:
        raise ConfigError("cddpt: GeoJSON object has no 'type' member")
    # A bare geometry (Point/Polygon/MultiPolygon/GeometryCollection/...).
    # shapely.shape() wants a concrete dict, not just any Mapping.
    return [shape(dict(data))]


def _looks_like_existing_file(value: str) -> bool:
    try:
        return Path(value).is_file()
    except OSError:
        return False


@dataclass(frozen=True, slots=True)
class Aoi:
    """An immutable area of interest, always in EPSG:4326 (WGS84)."""

    geometry: BaseGeometry

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """``(minx, miny, maxx, maxy)`` in WGS84 (lon/lat degrees)."""

        return self.geometry.bounds

    def to_geojson(self) -> dict[str, Any]:
        """This AOI's geometry as a GeoJSON geometry ``dict`` (WGS84)."""

        return _shapely_mapping(self.geometry)

    def area_km2(self) -> float:
        """This AOI's area in km², computed in EPSG:3763 (or a worldwide
        equal-area CRS if the AOI lies outside mainland Portugal)."""

        crs = _metric_crs_for_bounds(self.bounds)
        projected = _reproject(self.geometry, _WGS84, crs)
        return projected.area / 1_000_000.0

    def chunks(self, max_km2: float) -> list[Aoi]:
        """Split this AOI into a grid of sub-AOIs of at most ``max_km2``
        each, for search chunking (docs/PLAN.md, "AOI + search pagination").

        Builds a square grid (in EPSG:3763 metres, CDD's own tile-grid CRS)
        over this AOI's bounds, intersects each cell with the AOI, and
        returns each non-empty piece reprojected back to WGS84. A single
        chunk (``[self]``) is returned when the whole AOI is already at or
        under the threshold.
        """

        if max_km2 <= 0:
            raise ConfigError(f"cddpt: chunk max_km2 must be positive (got {max_km2})")
        if self.area_km2() <= max_km2:
            return [self]

        grid_crs = _metric_crs_for_bounds(self.bounds)
        geom_metric = _valid(_reproject(self.geometry, _WGS84, grid_crs))
        minx, miny, maxx, maxy = geom_metric.bounds
        side_m = math.sqrt(max_km2) * 1000.0

        pieces: list[Aoi] = []
        y = miny
        while y < maxy:
            y_top = min(y + side_m, maxy)
            x = minx
            while x < maxx:
                x_right = min(x + side_m, maxx)
                cell = box(x, y, x_right, y_top)
                piece = _polygonal_only(_valid(cell.intersection(geom_metric)))
                # > _MIN_CHUNK_AREA_M2 (not > 0): a WGS84<->metric CRS
                # round-trip can leave sub-millimetre floating-point noise
                # on geom_metric's bounds, which can otherwise manifest as
                # a spurious near-zero-area sliver chunk at a grid edge.
                if not piece.is_empty and piece.area > _MIN_CHUNK_AREA_M2:
                    pieces.append(Aoi(geometry=_valid(_reproject(piece, grid_crs, _WGS84))))
                x += side_m
            y += side_m

        return pieces if pieces else [self]

    # -- Constructors --------------------------------------------------

    @classmethod
    def from_bbox(cls, west: float, south: float, east: float, north: float) -> Aoi:
        if west >= east or south >= north:
            raise ConfigError(
                f"cddpt: invalid bbox ({west}, {south}, {east}, {north}): "
                "west must be < east and south must be < north"
            )
        return cls(geometry=_valid(box(west, south, east, north)))

    @classmethod
    def from_wkt(cls, wkt_str: str, crs: str = "EPSG:4326") -> Aoi:
        try:
            geom = shapely.from_wkt(wkt_str)
        except shapely.errors.ShapelyError as exc:
            raise ConfigError(f"cddpt: could not parse WKT: {exc}") from exc
        source_crs = _crs_from_string(crs)
        geom = _reproject(geom, source_crs, _WGS84)
        return cls(geometry=_valid(geom))

    @classmethod
    def from_geojson(cls, obj: ABCMapping[str, Any] | str | Path) -> Aoi:
        """Build an ``Aoi`` from a GeoJSON object, JSON text, or a path to a
        ``.geojson`` file.

        Honours a legacy top-level ``"crs"`` member (removed from the
        GeoJSON spec by RFC 7946 but still produced by some tools), e.g.::

            {"type": "...", "crs": {"type": "name",
                                     "properties": {"name": "urn:ogc:def:crs:EPSG::3763"}},
             ...}

        Unions all features' geometries when given a FeatureCollection.
        """

        data: ABCMapping[str, Any]
        if isinstance(obj, ABCMapping):
            data = obj
        else:
            text: str
            if isinstance(obj, Path) or _looks_like_existing_file(str(obj)):
                text = Path(obj).read_text(encoding="utf-8")
            else:
                text = obj
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"cddpt: could not parse GeoJSON: {exc}") from exc

        geometries = _extract_geometries(data)
        if not geometries:
            raise ConfigError("cddpt: GeoJSON contains no geometries")
        geom = shapely.union_all(geometries) if len(geometries) > 1 else geometries[0]

        crs_member = data.get("crs")
        if crs_member is not None:
            source_crs = _crs_from_legacy_geojson(crs_member)
            geom = _reproject(geom, source_crs, _WGS84)

        return cls(geometry=_valid(geom))

    @classmethod
    def from_file(cls, path: str | Path) -> Aoi:
        """Build an ``Aoi`` from any vector file geopandas/pyogrio can read
        (shapefile, GeoPackage, ...). Requires the optional ``cddpt[files]``
        extra (``geopandas`` + ``pyogrio``), imported lazily here."""

        try:
            # Not type-checked as a stub package: types-geopandas transitively
            # drags in a numpy stub version whose PEP 695 `type` aliases are
            # incompatible with this project's mypy python_version target.
            import geopandas as gpd  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ConfigError(
                "cddpt: reading AOIs from vector files requires the optional "
                "'files' extra. Install it with: pip install cddpt[files]"
            ) from exc

        gdf = gpd.read_file(path, engine="pyogrio")
        if gdf.crs is None:
            raise ConfigError(f"cddpt: {path}: file has no CRS; cannot determine AOI projection")
        gdf = gdf.to_crs(epsg=4326)
        geoms = [g for g in gdf.geometry if g is not None and not g.is_empty]
        if not geoms:
            raise ConfigError(f"cddpt: {path}: file contains no geometries")
        geom = shapely.union_all(geoms) if len(geoms) > 1 else geoms[0]
        return cls(geometry=_valid(geom))


__all__ = ["Aoi"]
