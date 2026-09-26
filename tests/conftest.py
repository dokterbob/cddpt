"""Shared pytest fixtures: vcrpy cassette configuration for cddpt's tests.

Cassettes under ``tests/cassettes/`` are real responses recorded against the
live, anonymous CDD API (see ``tests/test_catalog.py``), always THROUGH
``cddpt.http.make_session`` so the shared rate governor paces recording runs.
They replay offline by default (``record_mode="none"``): CI and normal
`pytest` runs never touch the network. To (re-)record, run with
``CDDPT_VCR_RECORD=once`` and delete the relevant cassette file(s) first
(vcrpy's "once" mode replays an existing cassette rather than re-recording
it).
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import vcr as vcr_module

CASSETTE_DIR = Path(__file__).parent / "cassettes"

#: Response header names to strip before a response is ever written to a
#: cassette -- vcrpy's own ``filter_headers`` option only scrubs *request*
#: headers, never response ones (verified against vcrpy 8.3.0's
#: ``VCR._build_before_record_request``/``_build_before_record_response``),
#: so DGT's session cookie needs its own ``before_record_response`` filter.
_RESPONSE_HEADERS_TO_SCRUB = {"set-cookie", "cookie"}


def _scrub_response_headers(response: Mapping[str, Any]) -> Mapping[str, Any]:
    headers = response.get("headers")
    if isinstance(headers, dict):
        for key in list(headers):
            if key.lower() in _RESPONSE_HEADERS_TO_SCRUB:
                del headers[key]
    return response


@pytest.fixture
def cdd_vcr() -> vcr_module.VCR:
    record_mode = os.environ.get("CDDPT_VCR_RECORD", "none")
    # match_on includes vcrpy's built-in "body" matcher (not just
    # method/URL): needed because /search POSTs differ only in JSON body
    # across AOI chunks and pagination tokens -- method+URL alone would
    # conflate them.
    return vcr_module.VCR(
        cassette_library_dir=str(CASSETTE_DIR),
        record_mode=record_mode,
        match_on=["method", "scheme", "host", "port", "path", "query", "body"],
        filter_headers=["Cookie", "Set-Cookie", "Authorization"],
        before_record_response=_scrub_response_headers,
        decode_compressed_response=True,
    )


@pytest.fixture
def cassette(request: pytest.FixtureRequest, cdd_vcr: vcr_module.VCR) -> Iterator[None]:
    """Use ``tests/cassettes/<test name>.yaml`` for the current test."""

    cassette_path = f"{request.node.name}.yaml"
    with cdd_vcr.use_cassette(cassette_path):
        yield
