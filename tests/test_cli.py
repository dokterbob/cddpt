"""Tests for cddpt's CLI (Milestone 3, part 1).

Fully offline via vcrpy cassettes (see ``tests/conftest.py``'s ``cassette``
fixture), except for the handful of one-time recordings noted below.

Cassette provenance
--------------------
- ``test_cli_collections_list_default/_all/_json.yaml`` and
  ``test_cli_collections_show[_json].yaml`` and
  ``test_cli_search_unknown_collection.yaml`` are byte-identical *copies* of
  Milestone 2's ``test_collections_envelope_shape.yaml`` /
  ``test_collection_detail_envelope_shape.yaml`` -- the underlying HTTP
  request (``GET /collections`` or ``GET /collections/{id}``) is exactly the
  same regardless of caller, so no new network access was needed for these.
- ``test_search_stream_formats_and_output.yaml``,
  ``test_search_aoi_geojson_file.yaml``, and ``test_search_estimate_only.yaml``
  were recorded once against the real, anonymous CDD API (small Lisbon-area
  AOIs, a handful of requests total -- ``CDDPT_VCR_RECORD=once uv run pytest
  tests/test_cli.py -k <name> --no-header``), matching this module's own
  ``cddpt.http.make_session``-governed session, and replay offline
  (``record_mode="none"``) from then on.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pystac
import pytest
from typer.testing import CliRunner

from cddpt import __version__
from cddpt.aoi import Aoi
from cddpt.cli import _common
from cddpt.cli.app import app
from cddpt.cli.search import OutputFormat, _print_estimate, _row_from_item, _stream_search
from cddpt.models import CollectionEstimate, SearchEstimate

FIXTURES = Path(__file__).parent / "fixtures"

#: A fast, effectively non-throttling rate policy for every CLI invocation
#: in this file -- same rationale as test_catalog.py's _FAST_SETTINGS.
_FAST_ENV = {"CDDPT_REQUESTS_PER_SECOND": "1000", "CDDPT_BURST": "1000"}

_LISBON_BBOX = "-9.15,38.70,-9.10,38.75"


def _normalize_ws(text: str) -> str:
    """Collapse all whitespace (including rich's help-text line wrapping) to
    single spaces, so a long disclaimer string can be found as a substring
    regardless of exactly where the terminal-width renderer wrapped it."""

    return " ".join(text.split())


@pytest.fixture(autouse=True)
def _reset_cli_state() -> None:
    """``_common``'s global CLI state is normally set by the top-level typer
    callback on every invocation -- reset it explicitly too, so a stray
    verbose=True can never leak from one test into the next (e.g. if a test
    invokes internals directly rather than through the CliRunner)."""

    _common.set_state(verbose=False, ca_bundle=None)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# --help / --version / disclaimer (no network)
# ---------------------------------------------------------------------------


def test_top_level_help_contains_disclaimer(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == _common.EXIT_OK
    assert _normalize_ws(_common.DISCLAIMER) in _normalize_ws(result.stdout)


@pytest.mark.parametrize(
    "args",
    [
        ["collections", "--help"],
        ["collections", "list", "--help"],
        ["collections", "show", "--help"],
        ["search", "--help"],
    ],
)
def test_every_command_help_contains_disclaimer(runner: CliRunner, args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == _common.EXIT_OK
    assert _normalize_ws(_common.DISCLAIMER) in _normalize_ws(result.stdout)


def test_version(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == _common.EXIT_OK
    assert __version__ in result.stdout


# ---------------------------------------------------------------------------
# collections list / show
# ---------------------------------------------------------------------------


def test_cli_collections_list_default(runner: CliRunner, cassette: None) -> None:
    result = runner.invoke(app, ["collections", "list"], env=_FAST_ENV)
    assert result.exit_code == _common.EXIT_OK, result.stderr
    assert "MDT-2m" in result.stdout
    assert "LAZ" in result.stdout
    # Hidden (Azores) collections are excluded by default.
    assert "ACORES-" not in result.stdout


def test_cli_collections_list_all(runner: CliRunner, cassette: None) -> None:
    result = runner.invoke(app, ["collections", "list", "--all"], env=_FAST_ENV)
    assert result.exit_code == _common.EXIT_OK, result.stderr
    assert "ACORES-" in result.stdout


def test_cli_collections_list_json(runner: CliRunner, cassette: None) -> None:
    result = runner.invoke(app, ["collections", "list", "--json"], env=_FAST_ENV)
    assert result.exit_code == _common.EXIT_OK, result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    by_id = {c["id"]: c for c in payload}
    assert "MDT-2m" in by_id
    # Verbatim license -- never asserted/rewritten by cddpt (see catalog.py).
    assert by_id["MDT-2m"]["license"] == "proprietary"
    assert not any(cid.startswith("ACORES-") for cid in by_id)


def test_cli_collections_show(runner: CliRunner, cassette: None) -> None:
    result = runner.invoke(app, ["collections", "show", "MDT-2m"], env=_FAST_ENV)
    assert result.exit_code == _common.EXIT_OK, result.stderr
    assert "MDT-2m" in result.stdout
    assert "proprietary" in result.stdout  # licence, verbatim


def test_cli_collections_show_json(runner: CliRunner, cassette: None) -> None:
    result = runner.invoke(app, ["collections", "show", "MDT-2m", "--json"], env=_FAST_ENV)
    assert result.exit_code == _common.EXIT_OK, result.stderr
    payload = json.loads(result.stdout)
    assert payload["id"] == "MDT-2m"
    assert payload["license"] == "proprietary"  # verbatim, never "other"
    assert payload["visibility"] == ["show"]


# ---------------------------------------------------------------------------
# search: usage errors (exit 2) -- no network
# ---------------------------------------------------------------------------


def test_search_missing_collection_is_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(app, ["search", "--bbox", _LISBON_BBOX])
    assert result.exit_code == _common.EXIT_USAGE


def test_search_no_aoi_is_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(app, ["search", "--collection", "LAZ"])
    assert result.exit_code == _common.EXIT_USAGE


def test_search_mutually_exclusive_aoi_is_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(
        app,
        ["search", "--bbox", _LISBON_BBOX, "--wkt", "POINT(0 0)", "--collection", "LAZ"],
    )
    assert result.exit_code == _common.EXIT_USAGE


def test_search_all_three_aoi_options_is_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(
        app,
        [
            "search",
            "--bbox",
            _LISBON_BBOX,
            "--wkt",
            "POINT(0 0)",
            "--aoi",
            "whatever.geojson",
            "--collection",
            "LAZ",
        ],
    )
    assert result.exit_code == _common.EXIT_USAGE


def test_search_bad_bbox_is_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(app, ["search", "--bbox", "not,a,bbox", "--collection", "LAZ"])
    assert result.exit_code == _common.EXIT_USAGE


# ---------------------------------------------------------------------------
# search: unknown collection id -- a clean CddError, not a usage error
# ---------------------------------------------------------------------------


def test_cli_search_unknown_collection(runner: CliRunner, cassette: None) -> None:
    result = runner.invoke(
        app,
        ["search", "--bbox", _LISBON_BBOX, "--collection", "NOPE"],
        env=_FAST_ENV,
    )
    assert result.exit_code == _common.EXIT_ERROR
    assert result.stdout == ""  # no partial/garbage data on stdout
    assert "unknown collection id" in result.stderr
    assert "NOPE" in result.stderr
    # The full list of valid ids is named, so the user can self-correct.
    assert "MDT-2m" in result.stderr


# ---------------------------------------------------------------------------
# search: streaming formats, --output, --estimate-only (network, recorded)
# ---------------------------------------------------------------------------


def test_search_stream_formats_and_output(
    runner: CliRunner, cassette: None, tmp_path: Path
) -> None:
    out_path = tmp_path / "out.geojson"

    table_result = runner.invoke(
        app,
        [
            "search",
            "--bbox",
            _LISBON_BBOX,
            "--collection",
            "LAZ",
            "--format",
            "table",
            "--output",
            str(out_path),
        ],
        env=_FAST_ENV,
    )
    assert table_result.exit_code == _common.EXIT_OK, table_result.stderr
    assert "Search results" in table_result.stdout
    assert "item(s) matched" in table_result.stderr
    assert "proprietary" in table_result.stderr  # licence reminder, verbatim

    # --output always writes a well-formed GeoJSON FeatureCollection,
    # independent of --format.
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["type"] == "FeatureCollection"
    assert payload["features"]
    for feature in payload["features"]:
        assert feature["type"] == "Feature"
        assert feature["geometry"] is not None
        assert feature["collection"] == "LAZ"

    json_result = runner.invoke(
        app,
        ["search", "--bbox", _LISBON_BBOX, "--collection", "LAZ", "--format", "json"],
        env=_FAST_ENV,
    )
    assert json_result.exit_code == _common.EXIT_OK, json_result.stderr
    lines = [line for line in json_result.stdout.splitlines() if line.strip()]
    assert lines
    for line in lines:
        item = json.loads(line)  # each line must be its own valid JSON object
        assert item["type"] == "Feature"
        assert item["collection"] == "LAZ"
    assert len(lines) == len(payload["features"])

    geojson_result = runner.invoke(
        app,
        ["search", "--bbox", _LISBON_BBOX, "--collection", "LAZ", "--format", "geojson"],
        env=_FAST_ENV,
    )
    assert geojson_result.exit_code == _common.EXIT_OK, geojson_result.stderr
    geojson_payload = json.loads(geojson_result.stdout)
    assert geojson_payload["type"] == "FeatureCollection"
    assert len(geojson_payload["features"]) == len(lines)


def test_search_aoi_geojson_file(runner: CliRunner, cassette: None) -> None:
    aoi_path = FIXTURES / "two_tiles_epsg3763.geojson"
    result = runner.invoke(
        app,
        ["search", "--aoi", str(aoi_path), "--collection", "MDT-2m", "--format", "json"],
        env=_FAST_ENV,
    )
    assert result.exit_code == _common.EXIT_OK, result.stderr
    assert "AOI area:" in result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    for line in lines:
        item = json.loads(line)
        assert item["collection"] == "MDT-2m"


def test_search_aoi_missing_file_is_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(
        app,
        ["search", "--aoi", "no-such-file.geojson", "--collection", "MDT-2m"],
    )
    assert result.exit_code == _common.EXIT_USAGE


def test_cli_search_estimate_only(runner: CliRunner, cassette: None) -> None:
    result = runner.invoke(
        app,
        ["search", "--bbox", _LISBON_BBOX, "--collection", "MDT-2m", "--estimate-only"],
        env=_FAST_ENV,
    )
    assert result.exit_code == _common.EXIT_OK, result.stderr
    assert "Estimate" in result.stdout
    assert "Total:" in result.stdout
    assert "item(s)" in result.stdout
    assert "proprietary" in result.stderr  # licence reminder, verbatim


# ---------------------------------------------------------------------------
# _print_estimate / _row_from_item: pure formatting unit tests (no network)
# ---------------------------------------------------------------------------


def _sample_estimate(total_known_bytes: int) -> SearchEstimate:
    return SearchEstimate(
        item_count=3,
        total_known_bytes=total_known_bytes,
        unknown_size_count=1,
        per_collection=(
            CollectionEstimate(
                collection_id="MDT-2m",
                item_count=3,
                known_bytes=total_known_bytes,
                unknown_size_count=1,
            ),
        ),
    )


def test_print_estimate_table(capsys: pytest.CaptureFixture[str]) -> None:
    _print_estimate(_sample_estimate(1_500_000_000), as_json=False)
    out = capsys.readouterr().out
    assert "MDT-2m" in out
    assert "1.5 GB" in out
    assert "Total:" in out
    assert "3 item(s)" in out
    assert "1 item(s) of unknown size" in out


def test_print_estimate_json(capsys: pytest.CaptureFixture[str]) -> None:
    _print_estimate(_sample_estimate(1_500_000_000), as_json=True)
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["item_count"] == 3
    assert payload["total_known_bytes"] == 1_500_000_000
    assert payload["unknown_size_count"] == 1
    assert payload["per_collection"][0]["collection_id"] == "MDT-2m"


def test_print_estimate_large_gets_rate_limit_note(capsys: pytest.CaptureFixture[str]) -> None:
    _print_estimate(_sample_estimate(_common.LARGE_ESTIMATE_BYTES + 1), as_json=False)
    out = capsys.readouterr().out
    assert "rate-limited" in out
    assert "intentional" in out


def test_print_estimate_small_has_no_rate_limit_note(capsys: pytest.CaptureFixture[str]) -> None:
    _print_estimate(_sample_estimate(1_000), as_json=False)
    out = capsys.readouterr().out
    assert "rate-limited" not in out


def test_row_from_item_decodable_tile_and_size() -> None:
    from datetime import datetime, timezone

    item = pystac.Item(
        id="MDT-2m-113194-07-2024",
        geometry={"type": "Point", "coordinates": [0, 0]},
        bbox=[0, 0, 0, 0],
        datetime=datetime(2024, 7, 16, tzinfo=timezone.utc),
        properties={"file:size": 1_500_000},
        collection="MDT-2m",
    )
    item_id, collection_id, tile, size, dt = _row_from_item(item)
    assert item_id == "MDT-2m-113194-07-2024"
    assert collection_id == "MDT-2m"
    assert tile == "113,194"
    assert size == "1.5 MB"
    assert dt.startswith("2024-07-16")


class _FakeCatalog:
    """A duck-typed stand-in for CddCatalog.iter_items() -- lets
    _stream_search's table-cap/streaming logic be tested without any
    network access or a real CddCatalog instance."""

    def __init__(self, items: list[pystac.Item]) -> None:
        self._items = items

    def iter_items(
        self, aoi: object, collections: object, *, datetime: object = None, chunk_km2: object = None
    ) -> object:
        yield from self._items


def _make_fake_item(index: int) -> pystac.Item:
    from datetime import datetime, timezone

    return pystac.Item(
        id=f"LO-113194-07-202{index % 10}",
        geometry={"type": "Point", "coordinates": [0, 0]},
        bbox=[0, 0, 0, 0],
        datetime=datetime(2020, 1, 1, tzinfo=timezone.utc),
        properties={"file:size": 100},
        collection="LAZ",
    )


def test_stream_search_caps_table_rows_but_counts_and_writes_all(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """search's table output caps rendered rows at MAX_TABLE_ROWS, but the
    returned count and --output's written FeatureCollection cover every
    item -- table rendering is display-only, never a silent data loss."""

    total = _common.MAX_TABLE_ROWS + 50
    items = [_make_fake_item(i) for i in range(total)]
    catalog = _FakeCatalog(items)
    aoi = Aoi.from_bbox(-9.15, 38.70, -9.10, 38.75)
    out_path = tmp_path / "out.geojson"

    count = _stream_search(
        catalog=catalog,  # type: ignore[arg-type]
        aoi=aoi,
        collection_ids=["LAZ"],
        datetime_=None,
        chunk_km2=None,
        output=out_path,
        format_=OutputFormat.table,
    )

    assert count == total
    out = capsys.readouterr().out
    assert "… 50 more" in out

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["type"] == "FeatureCollection"
    assert len(payload["features"]) == total


def test_row_from_item_undecodable_tile_and_unknown_size() -> None:
    from datetime import datetime, timezone

    item = pystac.Item(
        id="ORTOS-2021-cog-25cm-122-4",
        geometry={"type": "Point", "coordinates": [0, 0]},
        bbox=[0, 0, 0, 0],
        datetime=datetime(2021, 1, 1, tzinfo=timezone.utc),
        properties={},
        collection="ORTOS-2021",
    )
    _, _, tile, size, _ = _row_from_item(item)
    assert tile == "-"
    assert size == "unknown"


# ---------------------------------------------------------------------------
# human_size / license_reminder: pure unit tests (no network)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("num_bytes", "expected"),
    [
        (None, "unknown"),
        (0, "0 B"),
        (500, "500 B"),
        (68_200_000_000, "68.2 GB"),
        (1_400_000_000_000, "1.4 TB"),
    ],
)
def test_human_size(num_bytes: int | None, expected: str) -> None:
    assert _common.human_size(num_bytes) == expected


# ---------------------------------------------------------------------------
# --ca-bundle / -v --verbose: Settings + logging wiring (no network)
# ---------------------------------------------------------------------------


def test_ca_bundle_flag_propagates_to_settings(tmp_path: Path) -> None:
    ca_bundle = tmp_path / "ca.pem"
    ca_bundle.write_text("not a real cert, just a path\n", encoding="utf-8")

    _common.set_state(verbose=False, ca_bundle=ca_bundle)
    settings = _common.build_settings()
    assert settings.ca_bundle == ca_bundle


def test_no_ca_bundle_flag_leaves_settings_default() -> None:
    _common.set_state(verbose=False, ca_bundle=None)
    settings = _common.build_settings()
    assert settings.ca_bundle is None


def test_verbose_flag_sets_info_logging() -> None:
    import logging

    _common.configure_logging(verbose=True)
    assert logging.getLogger().level == logging.INFO

    _common.configure_logging(verbose=False)
    assert logging.getLogger().level == logging.WARNING


# ---------------------------------------------------------------------------
# Missing 'cli' extra: cddpt.cli.main() prints a clear message, exits 1
# ---------------------------------------------------------------------------


def test_main_missing_cli_extra_prints_clear_message_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cddpt import cli as cli_pkg

    # The documented way to simulate "this submodule cannot be imported"
    # without actually uninstalling typer/rich: CPython's import system
    # raises ImportError immediately for a name mapped to None in
    # sys.modules (see importlib._bootstrap._find_and_load).
    monkeypatch.setitem(sys.modules, "cddpt.cli.app", None)

    with pytest.raises(SystemExit) as excinfo:
        cli_pkg.main()

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "cddpt[cli]" in captured.err
    assert "pip install" in captured.err
