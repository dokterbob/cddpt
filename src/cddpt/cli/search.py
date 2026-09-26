"""``cddpt search`` -- AOI + collection driven STAC search, an
``--estimate-only`` mode, and GeoJSON export.

Thin formatting/streaming layer over :class:`cddpt.catalog.CddCatalog` and
:class:`cddpt.aoi.Aoi` -- no STAC/HTTP/AOI logic lives here.
"""

from __future__ import annotations

import json
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated, TextIO

import pystac
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..aoi import Aoi
from ..catalog import CddCatalog
from ..errors import ConfigError
from ..models import CollectionInfo, SearchEstimate
from ..tiles import decode_tile_key
from . import _common

_stdout = Console()
_stderr = Console(stderr=True)


class OutputFormat(str, Enum):
    """``--format`` choices for ``search``'s stdout results."""

    table = "table"
    json = "json"
    geojson = "geojson"


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    parts = value.split(",")
    if len(parts) != 4:
        raise typer.BadParameter(f"--bbox must be W,S,E,N (got {value!r})")
    try:
        west, south, east, north = (float(p.strip()) for p in parts)
    except ValueError as exc:
        raise typer.BadParameter(f"--bbox values must be numbers (got {value!r})") from exc
    return west, south, east, north


def _build_aoi(bbox: str | None, aoi_file: Path | None, wkt: str | None) -> Aoi:
    given = [
        name
        for name, value in (("--bbox", bbox), ("--aoi", aoi_file), ("--wkt", wkt))
        if value is not None
    ]
    if len(given) == 0:
        raise typer.BadParameter("exactly one of --bbox, --aoi, or --wkt is required")
    if len(given) > 1:
        raise typer.BadParameter(
            f"--bbox, --aoi, and --wkt are mutually exclusive (got {', '.join(given)})"
        )

    if bbox is not None:
        return Aoi.from_bbox(*_parse_bbox(bbox))
    if wkt is not None:
        return Aoi.from_wkt(wkt)

    assert aoi_file is not None
    if not aoi_file.is_file():
        raise typer.BadParameter(f"--aoi: no such file: {aoi_file}")
    suffix = aoi_file.suffix.lower()
    if suffix in {".geojson", ".json"}:
        return Aoi.from_geojson(aoi_file)
    # Any other vector format (shapefile, GeoPackage, ...) needs the
    # optional cddpt[files] extra -- Aoi.from_file raises a clean
    # ConfigError itself if it's missing; let it propagate (see
    # cli/app.py's generic CddError handling).
    return Aoi.from_file(aoi_file)


def _validate_collections(catalog: CddCatalog, requested: list[str]) -> dict[str, CollectionInfo]:
    """Validate ``requested`` collection ids against the live
    ``/collections`` list, raising a clean :class:`~cddpt.errors.ConfigError`
    naming every valid id if any are unknown."""

    known = {c.id: c for c in catalog.collections()}
    unknown = [cid for cid in requested if cid not in known]
    if unknown:
        raise ConfigError(
            f"cddpt: unknown collection id(s): {', '.join(unknown)}. "
            f"Valid ids: {', '.join(sorted(known))}"
        )
    return known


def _row_from_item(item: pystac.Item) -> tuple[str, str, str, str, str]:
    tile_key = decode_tile_key(item.id)
    tile_str = f"{tile_key.tile_x},{tile_key.tile_y}" if tile_key is not None else "-"
    size = item.properties.get("file:size")
    size_str = _common.human_size(size) if isinstance(size, int | float) else "unknown"
    dt = item.datetime
    dt_str = dt.isoformat() if dt is not None else "-"
    return item.id, item.collection_id or "-", tile_str, size_str, dt_str


def _print_estimate(
    estimate: SearchEstimate,
    *,
    as_json: bool,
) -> None:
    if as_json:
        payload = {
            "item_count": estimate.item_count,
            "total_known_bytes": estimate.total_known_bytes,
            "total_known_size": _common.human_size(estimate.total_known_bytes),
            "unknown_size_count": estimate.unknown_size_count,
            "per_collection": [
                {
                    "collection_id": c.collection_id,
                    "item_count": c.item_count,
                    "known_bytes": c.known_bytes,
                    "known_size": _common.human_size(c.known_bytes),
                    "unknown_size_count": c.unknown_size_count,
                }
                for c in estimate.per_collection
            ],
        }
        # Plain json.dumps -- never rich here (see collections.py's note):
        # this must stay valid, unwrapped JSON when piped/redirected.
        typer.echo(json.dumps(payload))
        return

    table = Table(title="Estimate (no data downloaded)")
    table.add_column("Collection")
    table.add_column("Items", justify="right")
    table.add_column("Known size", justify="right")
    table.add_column("Unknown-size items", justify="right")
    for c in sorted(estimate.per_collection, key=lambda c: c.collection_id):
        table.add_row(
            c.collection_id,
            str(c.item_count),
            _common.human_size(c.known_bytes),
            str(c.unknown_size_count),
        )
    _stdout.print(table)

    summary = (
        f"Total: {estimate.item_count} item(s), "
        f"{_common.human_size(estimate.total_known_bytes)} of known size"
    )
    if estimate.unknown_size_count:
        summary += f", {estimate.unknown_size_count} item(s) of unknown size"
    _stdout.print(summary)

    if estimate.total_known_bytes > _common.LARGE_ESTIMATE_BYTES:
        _stdout.print(
            Panel(
                "Downloads are deliberately rate-limited (2 concurrent, ~2 requests/second) "
                "-- downloading an estimate this size may take hours to days. This is "
                "intentional: DGT has published no rate limits, so cddpt errs conservative "
                "rather than risk being blocked. See docs/PLAN.md's rate-limiting policy.",
                title="Note",
                style="yellow",
            )
        )


def _stream_search(
    *,
    catalog: CddCatalog,
    aoi: Aoi,
    collection_ids: list[str],
    datetime_: str | None,
    chunk_km2: float | None,
    output: Path | None,
    format_: OutputFormat,
) -> int:
    """Stream ``catalog.iter_items()`` once, fanning out to stdout (per
    ``format_``) and/or ``--output`` incrementally. Returns the total item
    count (every item is counted, even table rows beyond the display cap)."""

    out_fh: TextIO | None = None
    out_wrote_any = False
    if output is not None:
        out_fh = output.open("w", encoding="utf-8")
        out_fh.write('{"type": "FeatureCollection", "features": [')

    stdout_geojson_wrote_any = False
    if format_ == OutputFormat.geojson:
        sys.stdout.write('{"type": "FeatureCollection", "features": [')

    count = 0
    table_rows: list[tuple[str, str, str, str, str]] = []

    try:
        for item in catalog.iter_items(
            aoi, collection_ids, datetime=datetime_, chunk_km2=chunk_km2
        ):
            count += 1
            item_dict = item.to_dict()

            if format_ == OutputFormat.json:
                typer.echo(json.dumps(item_dict))
            elif format_ == OutputFormat.geojson:
                if stdout_geojson_wrote_any:
                    sys.stdout.write(",")
                sys.stdout.write(json.dumps(item_dict))
                stdout_geojson_wrote_any = True
            elif count <= _common.MAX_TABLE_ROWS:
                table_rows.append(_row_from_item(item))

            if out_fh is not None:
                if out_wrote_any:
                    out_fh.write(",")
                out_fh.write(json.dumps(item_dict))
                out_wrote_any = True
    finally:
        if format_ == OutputFormat.geojson:
            sys.stdout.write("]}\n")
            sys.stdout.flush()
        if out_fh is not None:
            out_fh.write("]}")
            out_fh.close()

    if format_ == OutputFormat.table:
        table = Table(title=f"Search results ({count} item(s))")
        table.add_column("ID")
        table.add_column("Collection")
        table.add_column("Tile (x,y)")
        table.add_column("Size", justify="right")
        table.add_column("Datetime")
        for row in table_rows:
            table.add_row(*row)
        _stdout.print(table)
        if count > len(table_rows):
            _stdout.print(f"… {count - len(table_rows)} more")

    return count


@_common.handle_errors
def search_command(
    bbox: Annotated[
        str | None, typer.Option("--bbox", help="AOI as W,S,E,N in WGS84 decimal degrees.")
    ] = None,
    aoi: Annotated[
        Path | None,
        typer.Option(
            "--aoi",
            help="AOI from a file: .geojson/.json needs no extra; other vector "
            r"formats (shapefile, GeoPackage, ...) need the 'cddpt\[files]' extra.",
        ),
    ] = None,
    wkt: Annotated[
        str | None, typer.Option("--wkt", help="AOI as a WKT geometry string (WGS84).")
    ] = None,
    collection: Annotated[
        list[str],
        typer.Option(
            "--collection",
            help="Collection id to search (repeatable; at least one required).",
        ),
    ] = [],  # noqa: B006 -- typer re-reads this default per invocation, never mutated
    datetime_: Annotated[
        str | None,
        typer.Option(
            "--datetime",
            help="STAC datetime filter: an instant, or 'start/end' interval "
            "('..' for an open end), RFC 3339.",
        ),
    ] = None,
    chunk_km2: Annotated[
        float | None,
        typer.Option(
            "--chunk-km2",
            help="Override the AOI chunk size (km²) used for search pagination "
            "(default: Settings.chunk_km2, 2000).",
        ),
    ] = None,
    estimate_only: Annotated[
        bool,
        typer.Option(
            "--estimate-only",
            help="Only count/size matching assets (CddCatalog.estimate()) -- "
            "nothing is streamed or written.",
        ),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="Write results as a GeoJSON FeatureCollection to this file "
            "(streamed incrementally, independent of --format).",
        ),
    ] = None,
    format_: Annotated[
        OutputFormat,
        typer.Option(
            "--format",
            help="stdout format for results ('table'/'json'/'geojson'; 'json' "
            "also applies to --estimate-only's summary).",
        ),
    ] = OutputFormat.table,
) -> None:
    the_aoi = _build_aoi(bbox, aoi, wkt)
    collection_ids = list(collection)
    if not collection_ids:
        raise typer.BadParameter("at least one --collection is required")

    settings = _common.build_settings()
    catalog = CddCatalog(settings=settings)
    known_collections = _validate_collections(catalog, collection_ids)

    effective_chunk_km2 = chunk_km2 if chunk_km2 is not None else settings.chunk_km2
    chunk_count = len(the_aoi.chunks(effective_chunk_km2))
    _stderr.print(
        f"AOI area: {the_aoi.area_km2():.2f} km² -- {chunk_count} search "
        f"chunk(s) (max {effective_chunk_km2:g} km²/chunk)"
    )

    if estimate_only:
        estimate = catalog.estimate(
            the_aoi, collection_ids, datetime=datetime_, chunk_km2=chunk_km2
        )
        _print_estimate(estimate, as_json=(format_ == OutputFormat.json))
    else:
        count = _stream_search(
            catalog=catalog,
            aoi=the_aoi,
            collection_ids=collection_ids,
            datetime_=datetime_,
            chunk_km2=chunk_km2,
            output=output,
            format_=format_,
        )
        _stderr.print(f"{count} item(s) matched.")

    _stderr.print(_common.license_reminder(known_collections, collection_ids))


__all__ = ["OutputFormat", "search_command"]
