"""``build-cog``'s command-line interface -- a thin layer over the pipeline
modules (:mod:`cog_recipe.merge`, :mod:`cog_recipe.convert`): parses
arguments, wires a `tqdm`-backed progress callback, formats output. All
actual raster logic lives in the library modules, which take no dependency
on `typer`/`tqdm` themselves (see ``__init__.py``'s module docstring) so
they can be called directly, or later folded into `cddpt` itself, without
this layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from tqdm import tqdm

from cog_recipe.convert import (
    DEFAULT_BLOCKSIZE as CONVERT_DEFAULT_BLOCKSIZE,
)
from cog_recipe.convert import (
    DEFAULT_COMPRESS as CONVERT_DEFAULT_COMPRESS,
)
from cog_recipe.convert import (
    DEFAULT_MAX_Z_ERROR as CONVERT_DEFAULT_MAX_Z_ERROR,
)
from cog_recipe.convert import (
    DEFAULT_OVERVIEW_RESAMPLING as CONVERT_DEFAULT_OVERVIEW_RESAMPLING,
)
from cog_recipe.convert import convert_directory
from cog_recipe.errors import CogRecipeError
from cog_recipe.grid import Bounds
from cog_recipe.merge import (
    DEFAULT_BLOCKSIZE,
    DEFAULT_COMPRESS,
    DEFAULT_ENSURE_OVERVIEW_RES,
    DEFAULT_MAX_Z_ERROR,
    DEFAULT_OVERVIEW_RESAMPLING,
)
from cog_recipe.merge import merge_tiles as _merge_tiles
from cog_recipe.progress import ProgressCallback, ProgressEvent

app = typer.Typer(
    name="build-cog",
    add_completion=False,
    no_args_is_help=True,
    help=(
        "Re-encode DGT CDD GeoTIFF tiles (from `cddpt download`) into a seamless "
        "Cloud-Optimized GeoTIFF. Unofficial companion tool; not affiliated with, "
        "endorsed by, or supported by Direção-Geral do Território."
    ),
)


def _parse_bbox(value: str | None) -> Bounds | None:
    if value is None:
        return None
    parts = value.split(",")
    if len(parts) != 4:
        raise typer.BadParameter("--bbox must be 'minx,miny,maxx,maxy'")
    try:
        minx, miny, maxx, maxy = (float(p) for p in parts)
    except ValueError as exc:
        raise typer.BadParameter(f"--bbox values must be numbers: {exc}") from None
    if minx >= maxx or miny >= maxy:
        raise typer.BadParameter("--bbox must have minx < maxx and miny < maxy")
    return (minx, miny, maxx, maxy)


def _tqdm_progress(bar: tqdm[None]) -> ProgressCallback:
    seen_stages: set[str] = set()

    def _callback(event: ProgressEvent) -> None:
        if event.stage not in seen_stages:
            seen_stages.add(event.stage)
            bar.write(f"[{event.stage}] {event.message}" if event.message else f"[{event.stage}]")
        if event.total:
            bar.total = event.total
            bar.n = event.current
            bar.set_description(event.stage)
            bar.refresh()

    return _callback


@app.command()
def merge(
    input_dir: Annotated[
        Path, typer.Argument(exists=True, file_okay=False, help="Tile directory.")
    ],
    output: Annotated[Path, typer.Argument(help="Output COG path.")],
    pattern: Annotated[str, typer.Option(help="Glob pattern for input tiles.")] = "*.tif",
    bbox: Annotated[
        str | None,
        typer.Option(
            help="Clip to 'minx,miny,maxx,maxy' (EPSG:3763), snapped to the "
            "--ensure-overview-res grid."
        ),
    ] = None,
    max_z_error: Annotated[
        float, typer.Option("--max-z-error", help="LERC max error (m); the lossy bound.")
    ] = DEFAULT_MAX_Z_ERROR,
    nodata: Annotated[
        float | None, typer.Option(help="Override nodata (default: source; -999 if unset).")
    ] = None,
    ensure_overview_res: Annotated[
        float | None,
        typer.Option(
            "--ensure-overview-res",
            help="Guarantee an overview level at exactly this resolution (m); "
            "pass 0 to disable and build a plain power-of-2 pyramid instead.",
        ),
    ] = DEFAULT_ENSURE_OVERVIEW_RES,
    compress: Annotated[str, typer.Option(help="COG compression profile.")] = DEFAULT_COMPRESS,
    blocksize: Annotated[int, typer.Option(help="Internal tile size (px).")] = DEFAULT_BLOCKSIZE,
    overview_resampling: Annotated[
        str, typer.Option(help="Resampling used to build overviews from the merged raster.")
    ] = DEFAULT_OVERVIEW_RESAMPLING,
    keep_intermediate: Annotated[
        bool, typer.Option(help="Keep the intermediate mosaic VRT and base GeoTIFF.")
    ] = False,
    gdal_num_threads: Annotated[
        str, typer.Option("--gdal-threads", help="GDAL_NUM_THREADS for the streaming copy steps.")
    ] = "ALL_CPUS",
) -> None:
    """Merge tiles into ONE seamless, native-resolution COG (the primary command).

    Builds a seamless mosaic first, then encodes it as a COG with a
    guaranteed overview at --ensure-overview-res (default 10 m) built with
    'average' resampling on the merged raster -- so that level has no seams
    at former tile boundaries. See the README for the `gdal_contour` recipe.
    """

    parsed_bbox = _parse_bbox(bbox)
    effective_ensure = ensure_overview_res or None

    with tqdm(total=0, unit="step") as bar:
        try:
            result = _merge_tiles(
                input_dir,
                output,
                pattern=pattern,
                bbox=parsed_bbox,
                max_z_error=max_z_error,
                nodata=nodata,
                ensure_overview_res=effective_ensure,
                compress=compress,
                blocksize=blocksize,
                overview_resampling=overview_resampling,
                keep_intermediate=keep_intermediate,
                gdal_num_threads=gdal_num_threads,
                progress=_tqdm_progress(bar),
            )
        except CogRecipeError as exc:
            typer.secho(f"build-cog merge: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from None

    typer.echo(
        f"Wrote {result.output_path} ({result.width}x{result.height} @ {result.resolution} m, "
        f"{result.tile_count} tiles, overview factors {result.overview_factors}, "
        f"{result.output_bytes / 1e6:.1f} MB)"
    )


@app.command("mosaic-10m", hidden=True)
def mosaic_10m_alias(
    input_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    output: Annotated[Path, typer.Argument()],
) -> None:
    """Deprecated alias for `merge` kept for docs/build-cog.md compatibility."""

    typer.secho(
        "build-cog mosaic-10m is deprecated; use `build-cog merge` (native resolution, "
        "with a guaranteed 10 m overview level) instead.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    merge(input_dir=input_dir, output=output)


@app.command()
def convert(
    input_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    output_dir: Annotated[Path, typer.Argument()],
    pattern: Annotated[str, typer.Option(help="Glob pattern for input tiles.")] = "*.tif",
    max_z_error: Annotated[float, typer.Option("--max-z-error")] = CONVERT_DEFAULT_MAX_Z_ERROR,
    compress: Annotated[str, typer.Option()] = CONVERT_DEFAULT_COMPRESS,
    blocksize: Annotated[int, typer.Option()] = CONVERT_DEFAULT_BLOCKSIZE,
    overview_resampling: Annotated[str, typer.Option()] = CONVERT_DEFAULT_OVERVIEW_RESAMPLING,
    nodata: Annotated[float | None, typer.Option()] = None,
    resume: Annotated[bool, typer.Option("--resume/--force")] = True,
    log_file: Annotated[Path | None, typer.Option()] = None,
    report_file: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Secondary command: per-tile COG conversion (1:1 output tree).

    Most users want `build-cog merge` instead -- a single seamless COG. This
    is for the case where independent per-tile COGs are themselves wanted.
    """

    with tqdm(unit="tile") as bar:

        def progress(event: ProgressEvent) -> None:
            if event.total:
                bar.total = event.total
            bar.n = event.current
            bar.set_description(event.message)
            bar.refresh()

        try:
            summary = convert_directory(
                input_dir,
                output_dir,
                pattern=pattern,
                max_z_error=max_z_error,
                compress=compress,
                blocksize=blocksize,
                overview_resampling=overview_resampling,
                nodata=nodata,
                resume=resume,
                log_file=log_file,
                report_file=report_file,
                progress=progress,
            )
        except CogRecipeError as exc:
            typer.secho(f"build-cog convert: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from None

    typer.echo(
        f"processed={summary.processed} skipped={summary.skipped} failed={summary.failed} "
        f"({summary.input_bytes / 1e6:.1f} MB -> {summary.output_bytes / 1e6:.1f} MB)"
    )
    if summary.failed:
        typer.echo(f"failures logged to {summary.failed_csv}", err=True)
        raise typer.Exit(code=1)


def main() -> None:
    """Entry point for the ``build-cog`` console script."""

    app()


if __name__ == "__main__":
    main()
