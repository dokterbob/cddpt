from __future__ import annotations

from pathlib import Path

import pytest

from cog_recipe.errors import DiskPreflightError
from cog_recipe.resources import (
    DEFAULT_BIGTIFF_THRESHOLD_BYTES,
    DEFAULT_POOL_SIZE,
    check_disk_space,
    choose_bigtiff,
    ensure_fd_capacity,
    estimate_final_cog_bytes,
    estimate_uncompressed_bytes,
    resolve_cache_mb,
    resolve_pool_size,
    total_ram_mb,
)

# --- pool size -----------------------------------------------------------


def test_resolve_pool_size_explicit_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GDAL_MAX_DATASET_POOL_SIZE", "42")
    assert resolve_pool_size(500) == 500


def test_resolve_pool_size_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GDAL_MAX_DATASET_POOL_SIZE", "777")
    assert resolve_pool_size(None) == 777


def test_resolve_pool_size_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GDAL_MAX_DATASET_POOL_SIZE", raising=False)
    assert resolve_pool_size(None) == DEFAULT_POOL_SIZE


def test_resolve_pool_size_ignores_bad_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GDAL_MAX_DATASET_POOL_SIZE", "not-a-number")
    assert resolve_pool_size(None) == DEFAULT_POOL_SIZE


# --- cache size ------------------------------------------------------------


def test_resolve_cache_mb_explicit_wins() -> None:
    assert resolve_cache_mb(123) == 123


def test_resolve_cache_mb_from_ram(monkeypatch: pytest.MonkeyPatch) -> None:
    import cog_recipe.resources as mod

    monkeypatch.setattr(mod, "total_ram_mb", lambda: 16384)
    assert resolve_cache_mb(None) == 4096  # 25% of 16 GB


def test_resolve_cache_mb_caps_at_default(monkeypatch: pytest.MonkeyPatch) -> None:
    import cog_recipe.resources as mod

    monkeypatch.setattr(mod, "total_ram_mb", lambda: 1024 * 1024)  # 1 TB
    assert resolve_cache_mb(None) == mod.DEFAULT_CACHE_MB_CAP


def test_resolve_cache_mb_fallback_when_ram_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    import cog_recipe.resources as mod

    monkeypatch.setattr(mod, "total_ram_mb", lambda: None)
    assert resolve_cache_mb(None) == mod.FALLBACK_CACHE_MB


def test_total_ram_mb_returns_int_or_none() -> None:
    # Whatever this machine reports, it must be a sane type.
    result = total_ram_mb()
    assert result is None or (isinstance(result, int) and result > 0)


# --- fd limit ---------------------------------------------------------------


def test_ensure_fd_capacity_no_op_when_already_sufficient(monkeypatch: pytest.MonkeyPatch) -> None:
    import cog_recipe.resources as mod

    class _FakeResource:
        RLIMIT_NOFILE = 7
        RLIM_INFINITY = -1

        @staticmethod
        def getrlimit(_which: int) -> tuple[int, int]:
            return (10_000, 10_000)

        @staticmethod
        def setrlimit(_which: int, _limits: tuple[int, int]) -> None:
            raise AssertionError("setrlimit should not be called when already sufficient")

    monkeypatch.setattr(mod, "resource", _FakeResource)
    assert ensure_fd_capacity(1000) is None


def test_ensure_fd_capacity_raises_soft_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    import cog_recipe.resources as mod

    calls: dict[str, tuple[int, int]] = {}

    class _FakeResource:
        RLIMIT_NOFILE = 7
        RLIM_INFINITY = -1

        @staticmethod
        def getrlimit(_which: int) -> tuple[int, int]:
            return (256, 100_000)

        @staticmethod
        def setrlimit(_which: int, limits: tuple[int, int]) -> None:
            calls["set"] = limits

    monkeypatch.setattr(mod, "resource", _FakeResource)
    warning = ensure_fd_capacity(1000, margin=64)
    assert warning is None
    assert calls["set"][0] >= 1064


def test_ensure_fd_capacity_warns_when_hard_limit_too_low(monkeypatch: pytest.MonkeyPatch) -> None:
    import cog_recipe.resources as mod

    class _FakeResource:
        RLIMIT_NOFILE = 7
        RLIM_INFINITY = -1

        @staticmethod
        def getrlimit(_which: int) -> tuple[int, int]:
            return (256, 500)  # hard limit below what's needed

        @staticmethod
        def setrlimit(_which: int, limits: tuple[int, int]) -> None:
            pass

    monkeypatch.setattr(mod, "resource", _FakeResource)
    warning = ensure_fd_capacity(1000, margin=64)
    assert warning is not None
    assert "hard limit" in warning


def test_ensure_fd_capacity_warns_when_setrlimit_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import cog_recipe.resources as mod

    class _FakeResource:
        RLIMIT_NOFILE = 7
        RLIM_INFINITY = -1

        @staticmethod
        def getrlimit(_which: int) -> tuple[int, int]:
            return (256, 100_000)

        @staticmethod
        def setrlimit(_which: int, _limits: tuple[int, int]) -> None:
            raise OSError("nope")

    monkeypatch.setattr(mod, "resource", _FakeResource)
    warning = ensure_fd_capacity(1000, margin=64)
    assert warning is not None
    assert "lower --pool-size" in warning


def test_ensure_fd_capacity_no_resource_module(monkeypatch: pytest.MonkeyPatch) -> None:
    import cog_recipe.resources as mod

    monkeypatch.setattr(mod, "resource", None)
    warning = ensure_fd_capacity(1000)
    assert warning is not None
    assert "resource" in warning


# --- BIGTIFF / size estimates ------------------------------------------------


def test_choose_bigtiff_below_threshold() -> None:
    assert choose_bigtiff(1024) == "IF_SAFER"


def test_choose_bigtiff_above_threshold() -> None:
    assert choose_bigtiff(DEFAULT_BIGTIFF_THRESHOLD_BYTES + 1) == "YES"


def test_estimate_uncompressed_bytes() -> None:
    assert estimate_uncompressed_bytes(1000, 2000, 4) == 1000 * 2000 * 4


def test_estimate_final_cog_bytes_upper_bounds_native_band() -> None:
    base = 1_000_000
    final = estimate_final_cog_bytes(base)
    assert final > base  # overview pyramid adds on top


# --- disk preflight -----------------------------------------------------------


def test_check_disk_space_passes_when_plenty_free(tmp_path: Path) -> None:
    base_dir = tmp_path / "base"
    final_dir = tmp_path / "final"
    summary = check_disk_space(
        base_dir=base_dir, final_dir=final_dir, base_bytes=1024, final_bytes=1024, force=False
    )
    assert "required" in summary or "needed" in summary


def test_check_disk_space_raises_when_insufficient(tmp_path: Path) -> None:
    base_dir = tmp_path / "base"
    final_dir = tmp_path / "final"
    huge = 10**18  # comfortably more than any real disk
    with pytest.raises(DiskPreflightError, match="exceeds"):
        check_disk_space(
            base_dir=base_dir, final_dir=final_dir, base_bytes=huge, final_bytes=huge, force=False
        )


def test_check_disk_space_force_bypasses(tmp_path: Path) -> None:
    base_dir = tmp_path / "base"
    final_dir = tmp_path / "final"
    huge = 10**18
    # Must not raise.
    check_disk_space(
        base_dir=base_dir, final_dir=final_dir, base_bytes=huge, final_bytes=huge, force=True
    )


def test_check_disk_space_separate_filesystems_checked_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os as os_mod

    import cog_recipe.resources as mod

    base_dir = tmp_path / "base"
    final_dir = tmp_path / "final"
    base_dir.mkdir()
    final_dir.mkdir()

    real_stat = os_mod.stat

    class _StDevOverride:
        """Proxies a real ``os.stat_result`` but overrides ``st_dev``.

        Needed because monkeypatching ``os.stat`` here patches the real,
        shared `os` module (not a copy) -- `Path.mkdir(exist_ok=True)`'s
        internal `is_dir()` check goes through the same patched function, so
        the fake result must still look like a real stat result (``st_mode``
        and friends) for anything other than the two directories under test.
        """

        def __init__(self, real: object, st_dev: int) -> None:
            self._real = real
            self.st_dev = st_dev

        def __getattr__(self, name: str) -> object:
            return getattr(self._real, name)

    def fake_stat(path: object, *args: object, **kwargs: object) -> object:
        real_result = real_stat(path, *args, **kwargs)  # type: ignore[arg-type]
        if str(path) == str(base_dir):
            return _StDevOverride(real_result, 1)
        if str(path) == str(final_dir):
            return _StDevOverride(real_result, 2)
        return real_result

    monkeypatch.setattr(mod.os, "stat", fake_stat)

    # base is fine, final is not -> only the final-dir problem is reported.
    huge = 10**18
    with pytest.raises(DiskPreflightError, match="final COG"):
        check_disk_space(
            base_dir=base_dir, final_dir=final_dir, base_bytes=1024, final_bytes=huge, force=False
        )
