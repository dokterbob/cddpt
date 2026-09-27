"""Runtime resource tuning for national-scale merges.

A full-width block row of a 0.5 m mainland-Portugal mosaic (~1.12M px wide)
touches 500+ 1 km source tiles -- well above both GDAL's own default
``GDAL_MAX_DATASET_POOL_SIZE`` (~100) and, on some systems, the default
per-process file-descriptor limit. Left alone, GDAL thrashes open/close on
the source VRT's tile handles instead of keeping the working set open. This
module centralises the sizing/preflight logic for that, plus the
``GDAL_CACHEMAX`` block cache, the ``BIGTIFF`` mode, and a disk-space
preflight -- all sized (or checked) before any tile data is streamed, so a
misconfigured national-scale run fails fast with a clear message instead of
hours into a 1+ TB write.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from cog_recipe.errors import DiskPreflightError

try:
    import resource
except ImportError:  # pragma: no cover - `resource` is POSIX-only
    resource = None  # type: ignore[assignment]

#: GDAL's own built-in default (~100 open datasets) is comfortably below the
#: 500+ source tiles a full-width 0.5 m block row can touch at national
#: scale -- this is *this tool's* default, applied via `rasterio.Env`, not a
#: change to GDAL's compiled-in default.
DEFAULT_POOL_SIZE = 1000

#: Upper cap on the auto-computed GDAL_CACHEMAX default, regardless of how
#: much RAM is detected (a very large machine shouldn't hand GDAL more than
#: this without being asked explicitly via --cache-mb).
DEFAULT_CACHE_MB_CAP = 8192

#: Conservative fallback GDAL_CACHEMAX (MB) when total RAM can't be detected.
FALLBACK_CACHE_MB = 4096

#: Headroom above `pool_size` reserved for the process's own open files
#: (output COG, intermediate base GeoTIFF, VRT, stdio, etc.).
FD_MARGIN = 64

#: Above this estimated *uncompressed* mosaic size, use BIGTIFF=YES outright
#: rather than letting GDAL guess (IF_SAFER); comfortably under the 4 GiB
#: classic-TIFF offset ceiling, with margin for the COG's own overview data.
DEFAULT_BIGTIFF_THRESHOLD_BYTES = 2 * 1024**3

#: Rough upper-bound multiplier for a COG's total size (native band +
#: overview pyramid) relative to the native band alone: a decimation-by-2
#: overview pyramid is a geometric series in pixel *area* (each level 1/4 the
#: area of the one below), which sums to 4/3 of the base; 1.34 rounds that up
#: slightly for safety margin.
DEFAULT_OVERVIEW_SIZE_FACTOR = 1.34


def resolve_pool_size(pool_size: int | None) -> int:
    """Resolve the effective ``GDAL_MAX_DATASET_POOL_SIZE``.

    Precedence: an explicit ``--pool-size`` value, then the
    ``GDAL_MAX_DATASET_POOL_SIZE`` environment variable (if set and a valid
    integer), then :data:`DEFAULT_POOL_SIZE`.
    """

    if pool_size is not None:
        return pool_size
    env = os.environ.get("GDAL_MAX_DATASET_POOL_SIZE")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return DEFAULT_POOL_SIZE


def total_ram_mb() -> int | None:
    """Total physical RAM in MiB, or ``None`` if it can't be determined.

    Uses ``os.sysconf`` (``SC_PAGE_SIZE`` * ``SC_PHYS_PAGES``) -- no
    `psutil` dependency. Some platforms (or sandboxes) don't expose one or
    both of these; callers must handle ``None``.
    """

    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        phys_pages = os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return None
    if page_size <= 0 or phys_pages <= 0:
        return None
    return (page_size * phys_pages) // (1024 * 1024)


def resolve_cache_mb(cache_mb: int | None) -> int:
    """Resolve the effective ``GDAL_CACHEMAX`` (MB).

    An explicit ``--cache-mb`` wins outright. Otherwise: ``min(25% of
    detected RAM, DEFAULT_CACHE_MB_CAP)``, or :data:`FALLBACK_CACHE_MB` if
    RAM can't be detected. Kept below 100000 (MB) deliberately: GDAL
    interprets ``GDAL_CACHEMAX`` values below that threshold as MB and
    larger ones as bytes, so an explicit ``--cache-mb`` above ~97 GB would be
    silently reinterpreted -- comfortably out of range for this tool's use.
    """

    if cache_mb is not None:
        return cache_mb
    ram_mb = total_ram_mb()
    if ram_mb is None:
        return FALLBACK_CACHE_MB
    return min(ram_mb // 4, DEFAULT_CACHE_MB_CAP)


def ensure_fd_capacity(pool_size: int, *, margin: int = FD_MARGIN) -> str | None:
    """Best-effort raise of the process's soft ``RLIMIT_NOFILE``.

    Raises the soft limit toward the hard limit if ``pool_size + margin``
    exceeds it. Never raises an exception itself -- returns a warning
    message describing the problem if it can't secure enough headroom
    (no `resource` module on this platform, raising failed, or the hard
    limit itself is too low), or ``None`` if capacity is fine.
    """

    needed = pool_size + margin
    if resource is None:  # pragma: no cover - exercised only on non-POSIX
        return (
            "cannot check or raise the file-descriptor limit on this platform "
            "(no `resource` module); make sure the process's open-file limit "
            f"comfortably exceeds {needed} before a national-scale run."
        )

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft >= needed:
        return None

    new_soft = needed if hard == resource.RLIM_INFINITY else min(hard, needed)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
    except (ValueError, OSError) as exc:
        return (
            f"GDAL_MAX_DATASET_POOL_SIZE={pool_size} wants a file-descriptor limit of "
            f"at least {needed}, but the soft limit is {soft} and raising it failed "
            f"({exc}); lower --pool-size or ask an administrator to raise the hard "
            "limit (`ulimit -Hn`)."
        )

    if new_soft < needed:
        return (
            f"raised the file-descriptor soft limit to {new_soft} (the process's hard "
            f"limit), which is still below the {needed} wanted for "
            f"GDAL_MAX_DATASET_POOL_SIZE={pool_size}; lower --pool-size or ask an "
            "administrator to raise the hard limit."
        )
    return None


def estimate_uncompressed_bytes(width: int, height: int, dtype_size: int) -> int:
    """Uncompressed byte size of a single-band ``width`` x ``height`` raster."""

    return width * height * dtype_size


def estimate_final_cog_bytes(
    base_uncompressed_bytes: int, *, overview_factor: float = DEFAULT_OVERVIEW_SIZE_FACTOR
) -> int:
    """Upper-bound estimate of the final COG's *uncompressed* size.

    Includes the overview pyramid (see :data:`DEFAULT_OVERVIEW_SIZE_FACTOR`).
    Deliberately an upper bound: the actual LERC_ZSTD-compressed file is
    normally far smaller.
    """

    return int(base_uncompressed_bytes * overview_factor)


def choose_bigtiff(
    uncompressed_bytes: int, *, threshold_bytes: int = DEFAULT_BIGTIFF_THRESHOLD_BYTES
) -> str:
    """``"YES"`` once the estimated uncompressed mosaic exceeds ``threshold_bytes``.

    Below the threshold, ``"IF_SAFER"`` lets GDAL decide -- cheaper for
    genuinely small outputs, which never need BIGTIFF's slightly larger
    offset fields.
    """

    return "YES" if uncompressed_bytes > threshold_bytes else "IF_SAFER"


def check_disk_space(
    *,
    base_dir: Path,
    final_dir: Path,
    base_bytes: int,
    final_bytes: int,
    force: bool = False,
) -> str:
    """Preflight peak-disk-usage check for a ``merge`` run.

    ``base_bytes``/``final_bytes`` are upper-bound *uncompressed* estimates
    of the intermediate base GeoTIFF and the final COG (see
    :func:`estimate_uncompressed_bytes` / :func:`estimate_final_cog_bytes`) --
    the actual LERC_ZSTD-compressed files are normally much smaller, so this
    is deliberately conservative.

    Raises :class:`~cog_recipe.errors.DiskPreflightError` unless ``force`` is
    true. Returns a human-readable summary either way, for progress
    reporting/logging.
    """

    base_dir.mkdir(parents=True, exist_ok=True)
    final_dir.mkdir(parents=True, exist_ok=True)

    base_usage = shutil.disk_usage(base_dir)
    same_fs = os.stat(base_dir).st_dev == os.stat(final_dir).st_dev

    problems: list[str] = []
    if same_fs:
        required = base_bytes + final_bytes
        summary = (
            f"disk preflight (upper-bound, uncompressed estimates): intermediate base "
            f"~{base_bytes / 1e9:.1f} GB + final COG ~{final_bytes / 1e9:.1f} GB "
            f"= {required / 1e9:.1f} GB required on {base_dir} "
            f"({base_usage.free / 1e9:.1f} GB free)"
        )
        if required > base_usage.free:
            problems.append(
                f"estimated peak usage {required / 1e9:.1f} GB exceeds "
                f"{base_usage.free / 1e9:.1f} GB free on {base_dir}"
            )
    else:
        final_usage = shutil.disk_usage(final_dir)
        summary = (
            f"disk preflight (upper-bound, uncompressed estimates, separate "
            f"filesystems): intermediate base ~{base_bytes / 1e9:.1f} GB needed on "
            f"{base_dir} ({base_usage.free / 1e9:.1f} GB free); final COG "
            f"~{final_bytes / 1e9:.1f} GB needed on {final_dir} "
            f"({final_usage.free / 1e9:.1f} GB free)"
        )
        if base_bytes > base_usage.free:
            problems.append(
                f"intermediate base needs ~{base_bytes / 1e9:.1f} GB but only "
                f"{base_usage.free / 1e9:.1f} GB is free on {base_dir} (use --tmp-dir to "
                "place it on a different disk)"
            )
        if final_bytes > final_usage.free:
            problems.append(
                f"final COG needs ~{final_bytes / 1e9:.1f} GB but only "
                f"{final_usage.free / 1e9:.1f} GB is free on {final_dir}"
            )

    if problems and not force:
        raise DiskPreflightError(
            "; ".join(problems)
            + ". These are conservative upper bounds (actual LERC_ZSTD-compressed usage "
            "is normally far lower); free up space, pass --tmp-dir to split the "
            "intermediate base and final COG across disks, or pass --force to proceed "
            "anyway."
        )
    return summary
