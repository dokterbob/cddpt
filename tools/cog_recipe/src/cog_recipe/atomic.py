"""Atomic "write .tmp, validate, os.replace" helper shared by both commands.

"Exists at the final path" must always imply "validated": a `.tmp` that
fails validation is deleted, never renamed into place.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

TMP_SUFFIX = ".tmp"


def tmp_path_for(final_path: Path) -> Path:
    return final_path.with_name(final_path.name + TMP_SUFFIX)


def cleanup_stray_tmp(directory: Path) -> list[Path]:
    """Remove orphaned ``*.tmp`` files from a previous interrupted run.

    Returns the list of removed paths (for logging). Safe by construction:
    a `.tmp` file existing at all means its writer never reached the
    validated `os.replace`, so it can never be a partially-consumed valid
    output.
    """

    removed = []
    for tmp in directory.glob(f"*{TMP_SUFFIX}"):
        tmp.unlink()
        removed.append(tmp)
    return removed


def atomic_write(
    final_path: Path,
    writer: Callable[[Path], None],
    validator: Callable[[Path], None],
) -> None:
    """Write to ``final_path``'s `.tmp` sibling, validate, then replace.

    ``writer(tmp_path)`` produces the file; ``validator(tmp_path)`` raises
    on failure. On any exception the `.tmp` file is removed and the
    exception re-raised -- the final path is left untouched either way.
    """

    tmp_path = tmp_path_for(final_path)
    try:
        writer(tmp_path)
        validator(tmp_path)
        os.replace(tmp_path, final_path)
    except BaseException:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
