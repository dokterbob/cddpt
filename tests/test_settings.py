"""Tests for cddpt.settings.Settings."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from pydantic import ValidationError

from cddpt import __version__
from cddpt.settings import Settings


def test_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.api_base_url == "https://cdd.dgterritorio.gov.pt/dgt-be/v1"
    assert settings.site_base_url == "https://cdd.dgterritorio.gov.pt"
    assert settings.ca_bundle is None
    assert settings.requests_per_second == 2.0
    assert settings.burst == 4
    assert settings.concurrency == 2
    assert settings.chunk_km2 == 2000.0
    assert settings.connect_timeout == 10.0
    assert settings.read_timeout == 120.0
    assert isinstance(settings.config_dir, Path)
    assert isinstance(settings.cache_dir, Path)
    assert isinstance(settings.state_dir, Path)


def test_user_agent_mentions_version_and_is_generic() -> None:
    settings = Settings(_env_file=None)

    assert settings.user_agent.startswith("cddpt/")
    assert __version__ in settings.user_agent
    assert "+https://" in settings.user_agent


def test_env_prefix_overrides_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDDPT_API_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("CDDPT_REQUESTS_PER_SECOND", "5.5")

    settings = Settings(_env_file=None)

    assert settings.api_base_url == "https://example.test/v1"
    assert settings.requests_per_second == 5.5


def test_ca_bundle_accepts_path(tmp_path: Path) -> None:
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("fake cert data")

    settings = Settings(_env_file=None, ca_bundle=ca_file)

    assert settings.ca_bundle == ca_file


@pytest.mark.parametrize("value", [1, 2])
def test_concurrency_within_recommended_range_is_silent(value: int) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        settings = Settings(_env_file=None, concurrency=value)

    assert settings.concurrency == value


@pytest.mark.parametrize("value", [3, 4])
def test_concurrency_above_recommended_warns_but_is_accepted(value: int) -> None:
    with pytest.warns(UserWarning, match="concurrency"):
        settings = Settings(_env_file=None, concurrency=value)

    assert settings.concurrency == value


@pytest.mark.parametrize("value", [0, 5, 100])
def test_concurrency_outside_hard_cap_is_rejected(value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, concurrency=value)
