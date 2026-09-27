from __future__ import annotations

import pytest

from cog_recipe.errors import GdalCapabilityError
from cog_recipe.gdal_support import check_lerc_zstd_support


def test_lerc_zstd_supported_on_this_machine() -> None:
    """rasterio's bundled GDAL on this dev machine supports LERC_ZSTD."""

    check_lerc_zstd_support()  # must not raise


def test_lerc_zstd_preflight_failure_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """A GDAL build lacking LERC_ZSTD fails fast with one clear message."""

    import cog_recipe.gdal_support as mod

    class _BoomMemoryFile:
        def __enter__(self) -> _BoomMemoryFile:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def open(self, **kwargs: object) -> None:
            raise Exception("Unknown compression method LERC_ZSTD")

    monkeypatch.setattr(mod, "MemoryFile", _BoomMemoryFile)

    with pytest.raises(GdalCapabilityError) as excinfo:
        check_lerc_zstd_support()

    message = str(excinfo.value)
    assert "LERC_ZSTD" in message
    assert "rasterio" in message.lower() or "GDAL" in message
