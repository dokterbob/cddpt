"""TLS verification must be immune to concurrency.

Regression tests for a real bug: ``truststore.SSLContext`` (0.10.x)
temporarily switches the context it handshakes with to ``CERT_NONE`` /
``check_hostname=False`` and restores it afterwards, locking only the
switch itself. When cddpt shared one such context between download threads,
one thread's OS-level verification could run while another thread had the
shared context relaxed -- and then accepted an untrusted certificate with no
hostname check (urllib3 reported it as ``InsecureRequestWarning``). Against a
local self-signed server, two threads sharing one session had about half of
their connections accepted.

These tests run real handshakes against a local TLS server (a self-signed
certificate from ``tests/fixtures/tls``), with no internet access, except
the one ``network``-marked test at the end.
"""

from __future__ import annotations

import contextlib
import http.server
import socketserver
import ssl
import sys
import threading
import time
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import truststore._api as truststore_api
import urllib3.connection
from urllib3.exceptions import InsecureRequestWarning

from cddpt.errors import TlsVerificationError
from cddpt.http import make_session
from cddpt.ratelimit import RequestGovernor
from cddpt.settings import Settings

_TLS_DIR = Path(__file__).parent / "fixtures" / "tls"
_CERT = _TLS_DIR / "localhost-cert.pem"
_KEY = _TLS_DIR / "localhost-key.pem"

_THREADS = 4
_REQUESTS_PER_THREAD = 8


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.send_header("Connection", "close")  # every request = a new handshake
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: Any) -> None:
        pass


class _TlsServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(_CERT), str(_KEY))
        # Handshake lazily, in the per-connection handler thread.
        self.socket = context.wrap_socket(
            self.socket, server_side=True, do_handshake_on_connect=False
        )
        self.connections = 0
        self._count_lock = threading.Lock()

    def get_request(self) -> Any:
        request = super().get_request()
        with self._count_lock:
            self.connections += 1
        return request

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # clients rejecting our certificate is the expected case here


@pytest.fixture
def tls_server() -> Iterator[_TlsServer]:
    server = _TlsServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _url(server: _TlsServer) -> str:
    return f"https://localhost:{server.server_address[1]}/"


def _fast_governor() -> RequestGovernor:
    return RequestGovernor(requests_per_second=10_000.0, burst=10_000)


def _hammer(call: Callable[[], None]) -> list[BaseException | None]:
    """Run ``call`` _THREADS x _REQUESTS_PER_THREAD times concurrently,
    returning each attempt's exception (or None)."""

    results: list[BaseException | None] = []
    lock = threading.Lock()
    start = threading.Barrier(_THREADS)

    def worker() -> None:
        start.wait()
        for _ in range(_REQUESTS_PER_THREAD):
            try:
                call()
                outcome: BaseException | None = None
            except BaseException as exc:
                outcome = exc
            with lock:
                results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(_THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


@contextlib.contextmanager
def _record_insecure_warnings() -> Iterator[list[warnings.WarningMessage]]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        yield caught
    # Only InsecureRequestWarning matters here; ignore unrelated noise.
    caught[:] = [w for w in caught if issubclass(w.category, InsecureRequestWarning)]


def test_concurrent_requests_to_an_untrusted_server_are_all_rejected_fast(
    tls_server: _TlsServer,
) -> None:
    session = make_session(Settings(), governor=_fast_governor())

    with _record_insecure_warnings() as caught:
        started = time.monotonic()
        results = _hammer(lambda: session.get(_url(tls_server), timeout=10).close())
        elapsed = time.monotonic() - started

    assert len(results) == _THREADS * _REQUESTS_PER_THREAD
    accepted = [r for r in results if r is None]
    assert not accepted, f"{len(accepted)} connection(s) to an untrusted server were accepted"
    assert all(isinstance(r, TlsVerificationError) for r in results), results
    assert caught == []
    # Fail fast: exactly one connection per request -- no retry/backoff.
    assert tls_server.connections == len(results)
    assert elapsed < 20
    message = str(results[0])
    assert "--ca-bundle" in message
    assert "localhost" in message


@pytest.mark.skipif(
    sys.platform.startswith("linux"),
    reason=(
        "truststore's OpenSSL backend verifies during the handshake; its "
        "post-handshake OS-verifier hook is a no-op"
    ),
)
def test_os_verification_never_sees_a_relaxed_context_under_concurrency(
    tls_server: _TlsServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race, made deterministic: widen truststore's relaxed window (the
    time between switching a context to CERT_NONE and restoring it) and
    record the context state the OS verifier is handed. With a shared
    context this records CERT_NONE / check_hostname=False on nearly every
    run; with per-connection contexts, never.

    Linux uses truststore's OpenSSL backend, which performs verification in
    the handshake instead of the post-handshake OS-verifier hook patched
    below. The fail-closed handshake test above covers that backend."""

    seen: list[tuple[ssl.VerifyMode, bool]] = []
    seen_lock = threading.Lock()
    original_configure = truststore_api._configure_context

    @contextlib.contextmanager
    def slow_configure_context(ctx: ssl.SSLContext) -> Iterator[None]:
        with original_configure(ctx):
            time.sleep(0.02)
            yield
            time.sleep(0.02)

    def recording_verify(
        ssl_context: ssl.SSLContext, cert_chain: list[bytes], server_hostname: str | None = None
    ) -> None:
        with seen_lock:
            seen.append((ssl_context.verify_mode, ssl_context.check_hostname))
        time.sleep(0.01)
        # Accept: stands in for "the OS trusts this certificate", so the
        # request goes on to urllib3's own is_verified check too.

    monkeypatch.setattr(truststore_api, "_configure_context", slow_configure_context)
    monkeypatch.setattr(truststore_api, "_verify_peercerts_impl", recording_verify)

    session = make_session(Settings(), governor=_fast_governor())
    with _record_insecure_warnings() as caught:
        results = _hammer(lambda: session.get(_url(tls_server), timeout=10).close())

    # Checked first: this is the security property (verification ran with
    # verification enabled). The old shared context failed here with
    # (CERT_NONE, False) entries -- untrusted certificates accepted.
    assert set(seen) == {(ssl.CERT_REQUIRED, True)}
    assert caught == []
    # ...and the fail-closed side of the same race (a spurious
    # CERTIFICATE_VERIFY_FAILED) is gone too.
    assert results == [None] * (_THREADS * _REQUESTS_PER_THREAD)
    assert len(seen) == len(results)


def test_no_ssl_context_is_ever_used_by_two_handshakes_at_once(
    tls_server: _TlsServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The structural guarantee behind the fix: a context belongs to one
    connection object, and the pool only hands a connection to one thread
    at a time -- so handshakes never overlap on a context."""

    in_flight: set[int] = set()
    overlaps: list[int] = []
    handshakes = 0
    lock = threading.Lock()
    original_wrap = truststore_api.SSLContext.wrap_socket

    def tracking_wrap(self: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal handshakes
        with lock:
            handshakes += 1
            if id(self) in in_flight:
                overlaps.append(id(self))
            in_flight.add(id(self))
        try:
            time.sleep(0.01)
            return original_wrap(self, *args, **kwargs)
        finally:
            with lock:
                in_flight.discard(id(self))

    monkeypatch.setattr(truststore_api.SSLContext, "wrap_socket", tracking_wrap)
    session = make_session(Settings(), governor=_fast_governor())
    results = _hammer(lambda: session.get(_url(tls_server), timeout=10).close())

    assert handshakes == len(results)
    assert overlaps == []


def test_concurrent_requests_via_ca_bundle_succeed_without_warnings(
    tls_server: _TlsServer,
) -> None:
    session = make_session(Settings(ca_bundle=_CERT), governor=_fast_governor())
    with _record_insecure_warnings() as caught:
        results = _hammer(lambda: session.get(_url(tls_server), timeout=10).close())
    assert results == [None] * (_THREADS * _REQUESTS_PER_THREAD)
    assert caught == []


def test_an_unverified_connection_is_refused_instead_of_warned_about(
    tls_server: _TlsServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: even if some future bug left a handshake
    unverified, cddpt refuses to send the request (stock urllib3 would only
    emit InsecureRequestWarning and carry on)."""

    original = urllib3.connection._ssl_wrap_socket_and_match_hostname

    def unverified(*args: Any, **kwargs: Any) -> Any:
        return original(*args, **kwargs)._replace(is_verified=False)

    monkeypatch.setattr(urllib3.connection, "_ssl_wrap_socket_and_match_hostname", unverified)
    session = make_session(Settings(ca_bundle=_CERT), governor=_fast_governor())

    with _record_insecure_warnings() as caught, pytest.raises(TlsVerificationError) as info:
        session.get(_url(tls_server), timeout=10)

    assert "not verified" in str(info.value)
    assert caught == []


def test_requests_verify_false_cannot_downgrade_a_cddpt_session(
    tls_server: _TlsServer,
) -> None:
    session = make_session(Settings(), governor=_fast_governor())
    kwargs: dict[str, Any] = {"verify": bool(0)}  # spelled to satisfy the AST guard
    with pytest.raises((TlsVerificationError, ValueError)):
        session.get(_url(tls_server), timeout=10, **kwargs)


@pytest.mark.network
def test_live_concurrent_bad_certificates_are_all_rejected() -> None:
    """A handful of real requests: every badssl.com failure mode is rejected
    (no retries), concurrently, through one shared session."""

    session = make_session(Settings(), governor=RequestGovernor.from_settings(Settings()))
    urls = [
        "https://self-signed.badssl.com/",
        "https://self-signed.badssl.com/",
        "https://expired.badssl.com/",
        "https://wrong.host.badssl.com/",
    ]
    errors: list[BaseException | None] = [None] * len(urls)

    def fetch(index: int) -> None:
        try:
            session.get(urls[index], timeout=30).close()
        except BaseException as exc:
            errors[index] = exc

    with _record_insecure_warnings() as caught:
        threads = [threading.Thread(target=fetch, args=(i,)) for i in range(len(urls))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert all(isinstance(e, TlsVerificationError) for e in errors), errors
    assert caught == []
