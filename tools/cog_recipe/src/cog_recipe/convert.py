"""Secondary command: per-tile LERC_ZSTD COG conversion.

Superseded as the primary workflow by ``merge.py`` (see README "Deviations"):
a user who wants one seamless country/AOI-wide COG should reach for
``build-cog merge`` directly rather than convert-then-mosaic. This is kept
around, deliberately minimal (sequential, no multiprocessing), for the case
where per-tile COGs are themselves the wanted deliverable (e.g. serving
individual tiles to a tile-aware client that benefits from each file being
independently a COG).
"""

from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass
from pathlib import Path

import rasterio
from rio_cogeo.cogeo import cog_translate
from rio_cogeo.profiles import cog_profiles

from cog_recipe.atomic import atomic_write, cleanup_stray_tmp
from cog_recipe.gdal_support import check_lerc_zstd_support
from cog_recipe.progress import ProgressCallback, ProgressEvent, report
from cog_recipe.tileset import DEFAULT_NODATA, scan_tiles
from cog_recipe.validate import validate_cog

DEFAULT_MAX_Z_ERROR = 0.05
DEFAULT_COMPRESS = "lerc_zstd"
DEFAULT_BLOCKSIZE = 512
DEFAULT_OVERVIEW_RESAMPLING = "average"


@dataclass(frozen=True, slots=True)
class TileResult:
    source: Path
    dest: Path
    ok: bool
    skipped: bool = False
    error: str = ""


@dataclass(frozen=True, slots=True)
class ConvertSummary:
    processed: int
    skipped: int
    failed: int
    input_bytes: int
    output_bytes: int
    failed_csv: Path | None
    summary_json: Path | None


def convert_tile(
    source_path: Path,
    dest_path: Path,
    *,
    max_z_error: float = DEFAULT_MAX_Z_ERROR,
    compress: str = DEFAULT_COMPRESS,
    overview_resampling: str = DEFAULT_OVERVIEW_RESAMPLING,
    blocksize: int = DEFAULT_BLOCKSIZE,
    nodata: float | None = None,
    resume: bool = True,
) -> TileResult:
    """Convert one tile to a COG, atomically, with resume-by-existence."""

    if resume and dest_path.exists():
        return TileResult(source_path, dest_path, ok=True, skipped=True)

    try:
        with rasterio.open(source_path) as src:
            effective_nodata = nodata if nodata is not None else src.nodata
            if effective_nodata is None:
                effective_nodata = DEFAULT_NODATA
            expected_crs = src.crs
            expected_dtype = src.dtypes[0]
            expected_size = (src.width, src.height)

        dst_profile = cog_profiles.get(compress)  # type: ignore[no-untyped-call]
        dst_profile["MAX_Z_ERROR"] = max_z_error
        dst_profile["BLOCKXSIZE"] = blocksize
        dst_profile["BLOCKYSIZE"] = blocksize

        def writer(tmp_path: Path) -> None:
            cog_translate(
                str(source_path),
                str(tmp_path),
                dst_profile,
                overview_resampling=overview_resampling,  # type: ignore[arg-type]
                use_cog_driver=True,
                nodata=effective_nodata,
                web_optimized=False,
                quiet=True,
            )

        def validator(tmp_path: Path) -> None:
            validate_cog(
                tmp_path,
                expected_crs=expected_crs,
                expected_nodata=effective_nodata,
                expected_dtype=expected_dtype,
                expected_size=expected_size,
            )

        atomic_write(dest_path, writer, validator)
        return TileResult(source_path, dest_path, ok=True)
    except Exception as exc:
        return TileResult(source_path, dest_path, ok=False, error=str(exc))


def convert_directory(
    input_dir: Path,
    output_dir: Path,
    *,
    pattern: str = "*.tif",
    max_z_error: float = DEFAULT_MAX_Z_ERROR,
    compress: str = DEFAULT_COMPRESS,
    overview_resampling: str = DEFAULT_OVERVIEW_RESAMPLING,
    blocksize: int = DEFAULT_BLOCKSIZE,
    nodata: float | None = None,
    resume: bool = True,
    log_file: Path | None = None,
    report_file: Path | None = None,
    progress: ProgressCallback | None = None,
) -> ConvertSummary:
    """Convert every tile matching ``pattern`` under ``input_dir``, sequentially."""

    check_lerc_zstd_support()
    output_dir.mkdir(parents=True, exist_ok=True)
    cleanup_stray_tmp(output_dir)

    sources = scan_tiles(input_dir, pattern)
    log_file = log_file if log_file is not None else output_dir / "_failed.csv"
    report_file = report_file if report_file is not None else output_dir / "_summary.json"

    processed = skipped = failed = 0
    input_bytes = output_bytes = 0
    failed_rows: list[tuple[str, str, str]] = []

    total = len(sources)
    for i, source in enumerate(sources):
        dest = output_dir / source.name
        result = convert_tile(
            source,
            dest,
            max_z_error=max_z_error,
            compress=compress,
            overview_resampling=overview_resampling,
            blocksize=blocksize,
            nodata=nodata,
            resume=resume,
        )
        report(progress, ProgressEvent("convert", i + 1, total, source.name))
        if result.skipped:
            skipped += 1
        elif result.ok:
            processed += 1
            input_bytes += source.stat().st_size
            output_bytes += dest.stat().st_size
        else:
            failed += 1
            failed_rows.append((str(source), result.error, _now_iso()))

    if failed_rows:
        write_header = not log_file.exists()
        with log_file.open("a", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(["source_path", "error", "timestamp"])
            writer.writerows(failed_rows)

    summary = {
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
        "input_bytes": input_bytes,
        "output_bytes": output_bytes,
    }
    report_file.write_text(json.dumps(summary, indent=2))

    return ConvertSummary(
        processed=processed,
        skipped=skipped,
        failed=failed,
        input_bytes=input_bytes,
        output_bytes=output_bytes,
        failed_csv=log_file if failed_rows else None,
        summary_json=report_file,
    )


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
