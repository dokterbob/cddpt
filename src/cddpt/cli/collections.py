"""``cddpt collections list`` / ``cddpt collections show <id>``.

Thin formatting over :meth:`cddpt.catalog.CddCatalog.collections` and
:meth:`~cddpt.catalog.CddCatalog.collection` -- no catalog/HTTP logic here.
"""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from ..catalog import CddCatalog
from ..models import CollectionInfo
from . import _common

app = typer.Typer(
    name="collections",
    help=f"Browse CDD collections. {_common.DISCLAIMER}",
    no_args_is_help=True,
)

_stdout = Console()


def _extent_dict(info: CollectionInfo) -> dict[str, Any]:
    extent = info.collection.extent
    spatial = list(extent.spatial.bboxes) if extent.spatial else []
    temporal: list[list[str | None]] = []
    if extent.temporal:
        for start, end in extent.temporal.intervals:
            temporal.append(
                [
                    start.isoformat() if start is not None else None,
                    end.isoformat() if end is not None else None,
                ]
            )
    return {"spatial_bboxes": spatial, "temporal_intervals": temporal}


def _collection_to_dict(info: CollectionInfo) -> dict[str, Any]:
    return {
        "id": info.id,
        "title": info.title,
        "description": info.description,
        # Verbatim -- never asserted by cddpt. See catalog.py's docstring.
        "license": info.license,
        "visibility": list(info.visibility),
        "extent": _extent_dict(info),
    }


@app.command("list", help=f"List CDD collections. {_common.DISCLAIMER}")
@_common.handle_errors
def list_collections(
    all_: bool = typer.Option(
        False,
        "--all",
        help="Include hidden collections too (default: only downloadable/visible ones).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    settings = _common.build_settings()
    catalog = CddCatalog(settings=settings)
    collections = catalog.collections()
    if not all_:
        collections = [c for c in collections if "show" in c.visibility]
    collections = sorted(collections, key=lambda c: c.id)

    if json_output:
        # Plain json.dumps + typer.echo -- never rich's print_json here:
        # rich wraps/soft-truncates renderables to the console width, which
        # would silently corrupt machine-readable output piped to a file or
        # jq. Rich tables/panels below are for human eyes only.
        typer.echo(json.dumps([_collection_to_dict(c) for c in collections]))
        return

    title = "CDD collections"
    if not all_:
        title += " (downloadable only; use --all for hidden ones too)"
    table = Table(title=title)
    table.add_column("ID")
    table.add_column("Title")
    table.add_column("Visibility")
    table.add_column("License", style="dim")
    for info in collections:
        table.add_row(info.id, info.title or "", ", ".join(info.visibility) or "-", info.license)
    _stdout.print(table)


@app.command(
    "show",
    help="Show one collection's details, including its licence exactly as declared "
    f"by DGT (cddpt never asserts a licence of its own). {_common.DISCLAIMER}",
)
@_common.handle_errors
def show_collection(
    collection_id: str = typer.Argument(..., help="Collection ID, e.g. MDT-2m."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of a panel."),
) -> None:
    settings = _common.build_settings()
    catalog = CddCatalog(settings=settings)
    info = catalog.collection(collection_id)

    if json_output:
        typer.echo(json.dumps(_collection_to_dict(info)))
        return

    table = Table(title=f"Collection: {info.id}", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("ID", info.id)
    table.add_row("Title", info.title or "-")
    table.add_row("Description", info.description or "-")
    table.add_row("License (verbatim, as declared by DGT)", info.license)
    table.add_row("Visibility", ", ".join(info.visibility) or "-")

    extent = _extent_dict(info)
    bboxes = extent["spatial_bboxes"]
    table.add_row("Spatial extent", "; ".join(str(bbox) for bbox in bboxes) or "-")
    intervals = extent["temporal_intervals"]
    table.add_row(
        "Temporal extent",
        "; ".join(f"{start or '..'} / {end or '..'}" for start, end in intervals) or "-",
    )
    _stdout.print(table)


__all__ = ["app"]
