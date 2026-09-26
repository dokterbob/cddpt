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

import keyring
import keyring.backend
import keyring.errors
import pytest
import vcr as vcr_module
from keyring.compat import properties

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


@pytest.fixture(autouse=True)
def _isolate_cddpt_credential_env_vars(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strip ``CDDPT_USERNAME``/``CDDPT_PASSWORD`` from every test's
    environment, except the explicit ``@pytest.mark.network`` live test that
    is meant to use them.

    Every other (offline, mocked) auth test must be deterministic and must
    never accidentally observe real credentials from this task's own
    environment -- this autouse fixture is the single place that guarantees
    that, rather than relying on each test to remember to override
    ``Settings(username=None, password=None)`` itself.
    """

    if "network" in request.keywords:
        return
    monkeypatch.delenv("CDDPT_USERNAME", raising=False)
    monkeypatch.delenv("CDDPT_PASSWORD", raising=False)


# ---------------------------------------------------------------------------
# Milestone 4 (auth): a tiny in-memory keyring backend for offline tests.
#
# Never vcrpy/network for anything keyring-related -- this is a real (if
# trivial) keyring.backend.KeyringBackend subclass, activated via
# keyring.set_keyring() so cddpt.auth.store.CredentialStore exercises the
# real `keyring` public API end to end, just against a backend that never
# touches the OS.
# ---------------------------------------------------------------------------


class InMemoryKeyring(keyring.backend.KeyringBackend):
    """A ``dict``-backed keyring, for tests only."""

    @properties.classproperty
    def priority(cls) -> float:  # noqa: N805 -- classproperty, not a normal method
        return 1

    def __init__(self) -> None:
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self._store[(service, username)]
        except KeyError as exc:
            raise keyring.errors.PasswordDeleteError(f"{username!r} not found") from exc


@pytest.fixture
def fake_keyring() -> Iterator[InMemoryKeyring]:
    """Activate a fresh :class:`InMemoryKeyring` for the duration of one test,
    restoring whatever backend was active before (never the real OS keyring)."""

    previous = keyring.get_keyring()
    backend = InMemoryKeyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)


@pytest.fixture
def no_keyring_backend() -> Iterator[None]:
    """Activate keyring's own ``fail.Keyring`` backend, which raises
    :class:`keyring.errors.NoKeyringError` on every operation -- simulates a
    machine with no usable keyring backend at all."""

    import keyring.backends.fail as fail_backend

    previous = keyring.get_keyring()
    keyring.set_keyring(fail_backend.Keyring())
    try:
        yield
    finally:
        keyring.set_keyring(previous)
