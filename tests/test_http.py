"""Tests for cddpt.http: the single place TLS/session/retry are decided.

No real network access: a stub adapter (a plain
``requests.adapters.BaseAdapter``) is mounted in place of the real
TLS-verifying adapter wherever a request needs to actually "complete", and
the ``_GovernedRetry``/urllib3-retry-loop tests call urllib3's own
``Retry.increment()``/``.sleep()`` hooks directly with a synthetic wire
response, rather than going over a real (even loopback) socket.
"""

from __future__ import annotations

import ssl
import time
from collections.abc import Mapping
from typing import Any

import pytest
import requests
import truststore
import urllib3.util

import cddpt.http as http_module
from cddpt.errors import HttpError
from cddpt.http import GovernedSession, _build_ssl_context, make_session
from cddpt.ratelimit import RequestGovernor, ResponseLike
from cddpt.settings import Settings


class _FakeGovernor:
    """Records acquire()/observe()/observe_raw() calls instead of doing real
    rate limiting."""

    def __init__(self) -> None:
        self.acquire_calls = 0
        self.observed: list[ResponseLike] = []
        self.observed_raw: list[tuple[int, Mapping[str, str]]] = []

    def acquire(self) -> None:
        self.acquire_calls += 1

    def observe(self, response: ResponseLike) -> None:
        self.observed.append(response)

    def observe_raw(self, status_code: int, headers: Mapping[str, str]) -> None:
        self.observed_raw.append((status_code, headers))


class _FakeWireResponse:
    """Mimics the bits of a urllib3 ``BaseHTTPResponse`` that ``Retry`` hooks
    (``increment``/``sleep``) look at: ``.status``, ``.headers``, and
    ``.get_redirect_location()``."""

    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers: dict[str, str] = headers or {}

    def get_redirect_location(self) -> str | None:
        return None


class FakeClock:
    """A controllable clock: ``sleep()`` advances it instead of blocking.

    Mirrors ``tests/test_ratelimit.py``'s helper of the same name.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += seconds


class _DummyAdapter(requests.adapters.BaseAdapter):
    """A no-network adapter that records the kwargs it was called with."""

    def __init__(self, status_code: int = 200) -> None:
        super().__init__()
        self.status_code = status_code
        self.calls: list[dict[str, Any]] = []

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        self.calls.append(kwargs)
        response = requests.Response()
        response.status_code = self.status_code
        response.url = str(request.url)
        response.request = request
        response.headers.update({"Content-Type": "text/plain"})
        response._content = b""
        return response

    def close(self) -> None:  # pragma: no cover - nothing to release
        pass


# --------------------------------------------------------------------------
# TLS context selection
# --------------------------------------------------------------------------


def test_default_ssl_context_uses_truststore() -> None:
    settings = Settings(_env_file=None)
    ctx = _build_ssl_context(settings)
    assert isinstance(ctx, truststore.SSLContext)


def test_ca_bundle_uses_a_standard_verifying_context(monkeypatch: Any, tmp_path: Any) -> None:
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("not a real cert, just proving the code path")
    recorded: dict[str, Any] = {}

    def fake_create_default_context(*, cafile: str | None = None) -> ssl.SSLContext:
        recorded["cafile"] = cafile
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    monkeypatch.setattr(http_module.ssl, "create_default_context", fake_create_default_context)

    settings = Settings(_env_file=None, ca_bundle=ca_file)
    ctx = _build_ssl_context(settings)

    assert recorded["cafile"] == str(ca_file)
    assert isinstance(ctx, ssl.SSLContext)
    assert not isinstance(ctx, truststore.SSLContext)


# --------------------------------------------------------------------------
# Retry policy: exact values, and that _build_retry binds a governor
# --------------------------------------------------------------------------


def test_retry_kwargs_have_exact_configured_values() -> None:
    kwargs = http_module._RETRY_KWARGS
    assert kwargs["total"] == 5
    assert kwargs["backoff_factor"] == 2.0
    assert kwargs["backoff_max"] == 120
    assert kwargs["status_forcelist"] == (429, 500, 502, 503, 504)
    assert kwargs["respect_retry_after_header"] is True
    assert kwargs["allowed_methods"] == frozenset({"GET", "POST"})


def test_build_retry_produces_a_governed_retry_bound_to_the_governor() -> None:
    governor = _FakeGovernor()
    retry = http_module._build_retry(governor)  # type: ignore[arg-type]

    assert isinstance(retry, urllib3.util.Retry)
    assert isinstance(retry, http_module._GovernedRetry)
    assert retry._governor is governor
    assert retry.total == 5
    assert retry.backoff_factor == 2.0
    assert retry.backoff_max == 120
    assert retry.status_forcelist == (429, 500, 502, 503, 504)
    assert retry.respect_retry_after_header is True
    assert retry.allowed_methods == frozenset({"GET", "POST"})


# --------------------------------------------------------------------------
# _GovernedRetry: every wire-level retry (not just the final response) is
# governed -- this is what closes the gap urllib3's own retry loop (running
# *inside* HTTPAdapter.send(), below GovernedSession.send()) would otherwise
# leave open. Exercised by calling the same increment()/sleep() hooks
# urllib3's connectionpool.urlopen() calls, with a synthetic wire response,
# rather than a real socket.
# --------------------------------------------------------------------------


def test_governed_retry_observes_and_acquires_on_every_wire_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # urllib3's own exponential backoff (backoff_factor=2.0) uses the real
    # time.sleep and grows with each retry (4s, 8s, 16s, ...); neutralize it
    # so this test -- which deliberately drives 5 retries -- stays instant.
    # It is orthogonal to what's under test here (the governor hook).
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    governor = _FakeGovernor()
    retry: http_module._GovernedRetry = http_module._build_retry(governor)  # type: ignore[arg-type]
    response = _FakeWireResponse(status=429)

    for _ in range(5):
        retry = retry.increment(
            method="GET", url="https://cdd.example.test/", response=response, _pool=None
        )
        retry.sleep(response)

    assert governor.observed_raw == [(429, response.headers)] * 5
    assert governor.acquire_calls == 5


def test_governed_retry_raises_http_error_when_retries_are_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    governor = _FakeGovernor()
    retry: http_module._GovernedRetry = http_module._build_retry(governor)  # type: ignore[arg-type]
    response = _FakeWireResponse(status=429)

    for _ in range(5):  # total=5: exactly 5 retries are allowed
        retry = retry.increment(
            method="GET", url="https://cdd.example.test/", response=response, _pool=None
        )

    with pytest.raises(HttpError) as exc_info:
        retry.increment(
            method="GET", url="https://cdd.example.test/", response=response, _pool=None
        )

    assert exc_info.value.status == 429
    assert exc_info.value.url == "https://cdd.example.test/"
    # The exhausting (6th) attempt is still observed before HttpError is raised
    # -- this is exactly what lets a sustained 429 storm trip the breaker even
    # though requests never sees a response for that final attempt.
    assert len(governor.observed_raw) == 6


def test_five_429_retries_within_one_logical_request_trip_the_real_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    clock = FakeClock()
    governor = RequestGovernor(clock=clock.now, sleep=clock.sleep)
    retry: http_module._GovernedRetry = http_module._build_retry(governor)
    response = _FakeWireResponse(status=429)

    # increment() observes each response (and thus feeds the breaker) before
    # sleep() is ever called for that attempt, so the breaker is already open
    # after the 5th increment(). Skip the last sleep(): it would otherwise
    # immediately spend the fake clock's injected sleep() waiting out the
    # breaker's own 600s cooldown, closing it again before the assertion.
    for i in range(5):
        retry = retry.increment(
            method="GET", url="https://cdd.example.test/", response=response, _pool=None
        )
        if i < 4:
            retry.sleep(response)

    assert governor.breaker.is_open


def test_each_retry_attempt_consumes_a_shared_governor_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    clock = FakeClock()
    governor = RequestGovernor(clock=clock.now, sleep=clock.sleep, requests_per_second=2.0, burst=4)
    retry: http_module._GovernedRetry = http_module._build_retry(governor)
    # 500, not 429/503, so this proves token-bucket consumption specifically,
    # independent of the circuit breaker.
    response = _FakeWireResponse(status=500)

    start = clock.now()
    for _ in range(5):
        retry = retry.increment(
            method="GET", url="https://cdd.example.test/", response=response, _pool=None
        )
        retry.sleep(response)
    elapsed = clock.now() - start

    # The first 4 retries fit in the burst; the 5th must wait ~0.5s for a
    # token refill at the sustained 2/s rate -- proof that each wire-level
    # retry attempt really does draw from the same shared token bucket as an
    # ordinary (non-retried) request.
    assert elapsed == pytest.approx(0.5, abs=0.05)


# --------------------------------------------------------------------------
# GovernedSession.send(): default timeouts + governance wiring
# --------------------------------------------------------------------------


def test_send_applies_default_timeout_and_governs_request() -> None:
    governor = _FakeGovernor()
    session = GovernedSession(governor=governor, connect_timeout=7.0, read_timeout=42.0)  # type: ignore[arg-type]
    adapter = _DummyAdapter()
    session.mount("https://", adapter)

    response = session.get("https://cdd.example.test/ping")

    assert response.status_code == 200
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["timeout"] == (7.0, 42.0)
    assert governor.acquire_calls == 1
    assert governor.observed == [response]


def test_send_respects_an_explicit_timeout_override() -> None:
    governor = _FakeGovernor()
    session = GovernedSession(governor=governor, connect_timeout=7.0, read_timeout=42.0)  # type: ignore[arg-type]
    adapter = _DummyAdapter()
    session.mount("https://", adapter)

    session.get("https://cdd.example.test/ping", timeout=(1.0, 2.0))

    assert adapter.calls[0]["timeout"] == (1.0, 2.0)


def test_governor_observes_error_responses_too() -> None:
    governor = _FakeGovernor()
    session = GovernedSession(governor=governor, connect_timeout=7.0, read_timeout=42.0)  # type: ignore[arg-type]
    adapter = _DummyAdapter(status_code=429)
    session.mount("https://", adapter)

    response = session.get("https://cdd.example.test/ping")

    assert governor.observed == [response]
    assert governor.observed[0].status_code == 429


def test_multiple_requests_each_acquire_a_token() -> None:
    governor = _FakeGovernor()
    session = GovernedSession(governor=governor, connect_timeout=7.0, read_timeout=42.0)  # type: ignore[arg-type]
    session.mount("https://", _DummyAdapter())

    for _ in range(3):
        session.get("https://cdd.example.test/ping")

    assert governor.acquire_calls == 3


# --------------------------------------------------------------------------
# make_session(): full wiring
# --------------------------------------------------------------------------


def test_make_session_mounts_https_only_and_sets_user_agent() -> None:
    settings = Settings(_env_file=None)
    session = make_session(settings)

    https_adapter = session.get_adapter("https://cdd.dgterritorio.gov.pt/")
    assert isinstance(https_adapter, http_module._TruststoreAdapter)

    http_adapter = session.get_adapter("http://example.test/")
    assert not isinstance(http_adapter, http_module._TruststoreAdapter)

    assert session.headers["User-Agent"] == settings.user_agent


def test_make_session_uses_a_real_governor_by_default() -> None:
    settings = Settings(_env_file=None)
    session = make_session(settings)

    assert isinstance(session._governor, RequestGovernor)


def test_make_session_default_governor_honours_settings_rate_and_burst() -> None:
    settings = Settings(_env_file=None, requests_per_second=10.0, burst=7)

    session = make_session(settings)

    assert isinstance(session._governor, RequestGovernor)
    assert session._governor.requests_per_second == 10.0
    assert session._governor.burst == 7


def test_make_session_shares_the_given_governor() -> None:
    settings = Settings(_env_file=None)
    governor = _FakeGovernor()

    session = make_session(settings, governor=governor)  # type: ignore[arg-type]

    assert session._governor is governor
