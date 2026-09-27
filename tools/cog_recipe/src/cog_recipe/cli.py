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
from cog_recipe.grid import Bounds, union_bounds
from cog_recipe.merge import (
    DEFAULT_BLOCKSIZE,
    DEFAULT_COMPRESS,
    DEFAULT_ENSURE_OVERVIEW_RES,
    DEFAULT_MAX_Z_ERROR,
    DEFAULT_OVERVIEW_RESAMPLING,
)
from cog_recipe.merge import merge_tiles as _merge_tiles
from cog_recipe.progress import ProgressCallback, ProgressEvent
from cog_recipe.regions import compute_region_grid
from cog_recipe.tileset import scan_tile_bounds, scan_tiles

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
    cache_mb: Annotated[
        int | None,
        typer.Option(
            "--cache-mb",
            help="GDAL_CACHEMAX in MB for the whole pipeline (default: min(25% of detected "
            "RAM, 8192)).",
        ),
    ] = None,
    pool_size: Annotated[
        int | None,
        typer.Option(
            "--pool-size",
            help="GDAL_MAX_DATASET_POOL_SIZE -- how many source tiles GDAL keeps open at "
            "once. A full-width 0.5 m block row can touch 500+ tiles, well above GDAL's own "
            "~100 default (default here: the GDAL_MAX_DATASET_POOL_SIZE env var if set, "
            "else 1000). The process's file-descriptor limit is raised toward its hard "
            "limit if needed, with a warning if that's not enough headroom.",
        ),
    ] = None,
    tmp_dir: Annotated[
        Path | None,
        typer.Option(
            "--tmp-dir",
            help="Directory for the intermediate mosaic VRT + base GeoTIFF (default: "
            "OUTPUT's own directory). Point this at a different disk than OUTPUT to split "
            "the disk-space preflight's peak-usage estimate across filesystems.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Skip the disk-space preflight check."),
    ] = False,
    scan_workers: Annotated[
        int | None,
        typer.Option(
            "--scan-workers",
            help="Thread-pool size for the initial tile scan (default: ~4x CPU count, "
            "capped at 32 -- this is I/O-bound, so oversubscribing cores is intentional).",
        ),
    ] = None,
    fast_scan: Annotated[
        bool,
        typer.Option(
            "--fast-scan",
            help="Skip per-tile CRS/dtype/pixel-size/rotation cross-checks except on a "
            "sample (~200 tiles); trusts the rest to share the sampled tiles' grid. Only "
            "safe for a directory of consistently-named, already-trusted DGT tiles.",
        ),
    ] = False,
) -> None:
    """Merge tiles into ONE seamless, native-resolution COG (the primary command).

    Builds a seamless mosaic first, then encodes it as a COG with a
    guaranteed overview at --ensure-overview-res (default 10 m) built with
    'average' resampling on the merged raster -- so that level has no seams
    at former tile boundaries. See the README for the `gdal_contour` recipe,
    and docs/build-cog.md "National-scale runs" for --cache-mb/--pool-size/
    --tmp-dir/--fast-scan guidance on a 91k-tile national merge.
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
                cache_mb=cache_mb,
                pool_size=pool_size,
                tmp_dir=tmp_dir,
                force=force,
                scan_workers=scan_workers,
                fast_scan=fast_scan,
                progress=_tqdm_progress(bar),
            )
        except CogRecipeError as exc:
            typer.secho(f"build-cog merge: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from None

    typer.echo(
        f"Wrote {result.output_path} ({result.width}x{result.height} @ {result.resolution} m, "
        f"{result.tile_count} tiles, overview factors {result.overview_factors}, "
        f"{result.output_bytes / 1e6:.1f} MB, GDAL_CACHEMAX={result.gdal_cache_mb} MB, "
        f"GDAL_MAX_DATASET_POOL_SIZE={result.gdal_pool_size}, BIGTIFF={result.bigtiff})"
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


@app.command("plan-regions")
def plan_regions(
    input_dir: Annotated[
        Path, typer.Argument(exists=True, file_okay=False, help="Tile directory.")
    ],
    region_grid_km: Annotated[
        float, typer.Option("--region-grid", help="Region size in km (snapped up to the grid).")
    ],
    pattern: Annotated[str, typer.Option(help="Glob pattern for input tiles.")] = "*.tif",
    ensure_overview_res: Annotated[
        float,
        typer.Option(
            "--ensure-overview-res",
            help="Grid resolution (m) each region's bbox is snapped to -- pass the same "
            "value you'll use on each `merge` invocation.",
        ),
    ] = DEFAULT_ENSURE_OVERVIEW_RES,
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir", help="Directory the printed commands write each region's COG to."
        ),
    ] = Path("regions"),
    scan_workers: Annotated[
        int | None, typer.Option("--scan-workers", help="Thread-pool size for the bounds scan.")
    ] = None,
) -> None:
    """Print `build-cog merge --bbox ...` commands tiling INPUT_DIR into --region-grid km squares.

    Convenience for national-scale 0.5 m runs, where merging the whole
    country in one `merge` call is impractical (huge peak-disk footprint, a
    single multi-hour run with no incremental progress). Each printed
    command is a fully independent `build-cog merge` invocation --
    parallelizable across processes/machines/disks -- producing its own
    seamless-within-itself, grid-aligned COG. This command does not merge
    regions back together; run each printed command yourself. See
    docs/build-cog.md "National-scale runs".
    """

    paths = scan_tiles(input_dir, pattern)
    if not paths:
        typer.secho(
            f"build-cog plan-regions: no tiles matching {pattern!r} found in {input_dir}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    bounds_list = scan_tile_bounds(paths, max_workers=scan_workers)
    mosaic_bounds = union_bounds(bounds_list)
    regions = compute_region_grid(mosaic_bounds, region_grid_km * 1000.0, ensure_overview_res)

    typer.echo(
        f"# {len(paths)} tiles, mosaic bounds {mosaic_bounds}, "
        f"{len(regions)} region(s) of ~{region_grid_km} km snapped to the "
        f"{ensure_overview_res} m grid",
        err=True,
    )
    for region in regions:
        col, row = region.index
        minx, miny, maxx, maxy = region.bounds
        out_path = output_dir / f"region_{col:03d}_{row:03d}.tif"
        typer.echo(
            f"build-cog merge {input_dir} {out_path} --pattern {pattern!r} "
            f"--bbox {minx},{miny},{maxx},{maxy} --ensure-overview-res {ensure_overview_res}"
        )


def main() -> None:
    """Entry point for the ``build-cog`` console script."""

    app()


if __name__ == "__main__":
    main()
