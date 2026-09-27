"""Tests for :mod:`cddpt.naming` (Milestone 5): the media-type -> extension
map, path-traversal safety, and every :class:`~cddpt.naming.Layout`.

Fully offline, pure-function tests -- no network, no filesystem I/O beyond
:class:`pathlib.Path` construction (never touches disk).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from shapely.geometry import Point

from cddpt.models import AssetRef
from cddpt.naming import (
    DEFAULT_UNTILED_DIRNAME,
    ByCollectionLayout,
    ByTileLayout,
    FlatLayout,
    asset_extension,
    storage_filename,
)
from cddpt.tiles import TileKey

_POINT = Point(0, 0)


def _asset(
    *,
    item_id: str = "MDT-2m-111195-07-2024",
    collection_id: str = "MDT-2m",
    media_type: str | None = "image/tiff; application=geotiff",
    size_bytes: int | None = 1_000_000,
    tile_key: TileKey | None = None,
) -> AssetRef:
    return AssetRef(
        item_id=item_id,
        collection_id=collection_id,
        asset_key="data",
        href="https://cdd.dgterritorio.gov.pt/dgt-be/v1/download/sentinel-token",
        size_bytes=size_bytes,
        media_type=media_type,
        tile_key=tile_key,
        geometry=_POINT,
    )


# ---------------------------------------------------------------------------
# asset_extension / storage_filename: the media-type -> extension map
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("media_type", "expected"),
    [
        ("image/tiff", ".tif"),
        ("image/tiff; application=geotiff", ".tif"),
        ("image/tiff; application=geotiff; profile=cloud-optimized", ".tif"),
        ("application/vnd.laszip", ".laz"),
        (None, ".bin"),
        ("", ".bin"),
        ("application/x-totally-unknown-cddpt-test-type", ".bin"),
        # mimetypes' own guess is honoured for anything it recognises that
        # isn't in cddpt's own small table.
        ("application/json", ".json"),
    ],
)
def test_asset_extension(media_type: str | None, expected: str) -> None:
    assert asset_extension(_asset(media_type=media_type)) == expected


def test_storage_filename_is_item_id_plus_extension() -> None:
    asset = _asset(item_id="MDT-2m-111195-07-2024", media_type="image/tiff")
    assert storage_filename(asset) == "MDT-2m-111195-07-2024.tif"


def test_storage_filename_laz() -> None:
    asset = _asset(item_id="LO-111195-07-2024", media_type="application/vnd.laszip")
    assert storage_filename(asset) == "LO-111195-07-2024.laz"


# ---------------------------------------------------------------------------
# Path-traversal safety: a malformed/hostile item id or collection id must
# never escape a single path component.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile_id",
    [
        "../../etc/passwd",
        "..",
        ".",
        "a/b/c",
        "a\\b\\c",
        "",
        "   ",
        "....",
    ],
)
def test_storage_filename_never_escapes_a_path_component(hostile_id: str) -> None:
    asset = _asset(item_id=hostile_id)
    name = storage_filename(asset)
    assert "/" not in name
    assert "\\" not in name
    assert "\x00" not in name
    assert not name.startswith(".")
    assert name  # never empty


def test_by_collection_layout_hostile_collection_id_stays_a_single_component(
    tmp_path: Path,
) -> None:
    asset = _asset(collection_id="../../../etc")
    dest = ByCollectionLayout().dest_for(asset, tmp_path)
    # Exactly two path components were added under tmp_path: a sanitized
    # collection dir, then the filename -- never fewer (a traversal that
    # collapsed segments) or a path outside tmp_path.
    relative = dest.relative_to(tmp_path)
    assert len(relative.parts) == 2
    assert ".." not in relative.parts


# ---------------------------------------------------------------------------
# ByCollectionLayout (the default)
# ---------------------------------------------------------------------------


def test_by_collection_layout(tmp_path: Path) -> None:
    asset = _asset(collection_id="MDT-2m", item_id="MDT-2m-111195-07-2024")
    dest = ByCollectionLayout().dest_for(asset, tmp_path)
    assert dest == tmp_path / "MDT-2m" / "MDT-2m-111195-07-2024.tif"


# ---------------------------------------------------------------------------
# ByTileLayout
# ---------------------------------------------------------------------------


def test_by_tile_layout_decodes_from_item_id(tmp_path: Path) -> None:
    asset = _asset(item_id="MDT-2m-111195-07-2024")
    dest = ByTileLayout().dest_for(asset, tmp_path)
    assert dest == tmp_path / "111195" / "MDT-2m-111195-07-2024.tif"


def test_by_tile_layout_uses_precomputed_tile_key_when_present(tmp_path: Path) -> None:
    # AssetRef.tile_key, when already decoded (as CddCatalog.iter_assets
    # does), is used directly rather than re-decoding item_id -- exercise
    # that path explicitly with a tile_key that would decode differently
    # than the item_id alone (an artificial case, but proves the precedence).
    tile_key = TileKey(item_id="whatever", prefix="X", tile_x=42, tile_y=7, lot=1, year=2024)
    asset = _asset(item_id="not-a-tile-id-at-all", tile_key=tile_key)
    dest = ByTileLayout().dest_for(asset, tmp_path)
    assert dest.parent.name == "042007"


def test_by_tile_layout_fallback_for_undecodable_id(tmp_path: Path) -> None:
    asset = _asset(item_id="ORTOS-2021-cog-25cm-122-4", collection_id="ORTOS-2021")
    dest = ByTileLayout().dest_for(asset, tmp_path)
    assert dest.parent.name == DEFAULT_UNTILED_DIRNAME
    assert dest.name == "ORTOS-2021-cog-25cm-122-4.bin" or dest.name.startswith(
        "ORTOS-2021-cog-25cm-122-4"
    )


def test_by_tile_layout_custom_fallback_dirname(tmp_path: Path) -> None:
    asset = _asset(item_id="ORTOS-2021-cog-25cm-122-4")
    dest = ByTileLayout(fallback_dirname="other").dest_for(asset, tmp_path)
    assert dest.parent.name == "other"


# ---------------------------------------------------------------------------
# FlatLayout
# ---------------------------------------------------------------------------


def test_flat_layout(tmp_path: Path) -> None:
    asset = _asset(item_id="MDT-2m-111195-07-2024")
    dest = FlatLayout().dest_for(asset, tmp_path)
    assert dest == tmp_path / "MDT-2m-111195-07-2024.tif"


# ---------------------------------------------------------------------------
# Determinism: every Layout is a pure function of (asset, out_dir).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layout", [ByCollectionLayout(), ByTileLayout(), FlatLayout()])
def test_layout_is_deterministic(tmp_path: Path, layout: object) -> None:
    asset = _asset()
    first = layout.dest_for(asset, tmp_path)  # type: ignore[attr-defined]
    second = layout.dest_for(asset, tmp_path)  # type: ignore[attr-defined]
    assert first == second
