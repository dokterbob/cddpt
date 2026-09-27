"""A stalled transfer stream is detected and resumed via ``Range``.

Needs a real socket (the ``responses`` mocks used by ``test_download.py``
cannot stall mid-body), so this runs against a local TLS server trusted via
``ca_bundle`` (the self-signed certificate in ``tests/fixtures/tls``).
"""

from __future__ import annotations

import http.server
import logging
import socketserver
import ssl
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from shapely.geometry import Point

from cddpt.auth.base import AuthManager
from cddpt.catalog import CddCatalog
from cddpt.download import Downloader
from cddpt.models import AssetRef, DownloadStatus
from cddpt.naming import FlatLayout
from cddpt.ratelimit import RequestGovernor
from cddpt.settings import Settings

_TLS_DIR = Path(__file__).parent / "fixtures" / "tls"
_CERT = _TLS_DIR / "localhost-cert.pem"
_KEY = _TLS_DIR / "localhost-key.pem"

_SIZE = 1000
_FIRST_CHUNK = 400
_SERVER_STALL_SECONDS = 5.0
_BODY = bytes(range(256)) * 3 + bytes(range(232))
assert len(_BODY) == _SIZE


class _StallingHandler(http.server.BaseHTTPRequestHandler):
    """First request: 200, sends 400 of 1000 bytes, then goes silent.
    Any ``Range`` request: 206 with the rest."""

    protocol_version = "HTTP/1.1"
    range_requests: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        range_header = self.headers.get("Range")
        if range_header is None:
            self.send_response(200)
            self.send_header("Content-Length", str(_SIZE))
            self.end_headers()
            self.wfile.write(_BODY[:_FIRST_CHUNK])
            self.wfile.flush()
            time.sleep(_SERVER_STALL_SECONDS)
            self.close_connection = True
            return
        type(self).range_requests.append(range_header)
        start = int(range_header.removeprefix("bytes=").rstrip("-"))
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{_SIZE - 1}/{_SIZE}")
        self.send_header("Content-Length", str(_SIZE - start))
        self.end_headers()
        self.wfile.write(_BODY[start:])

    def log_message(self, format: str, *args: Any) -> None:
        pass


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass


@pytest.fixture
def stalling_server() -> Iterator[_Server]:
    _StallingHandler.range_requests = []
    server = _Server(("127.0.0.1", 0), _StallingHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(_CERT), str(_KEY))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


class _NoAuthProvider:
    def is_available(self) -> bool:
        return True

    def authenticate(self) -> Any:  # pragma: no cover - never reached
        raise AssertionError("no auth expected")

    def refresh(self, session: Any) -> Any:  # pragma: no cover - never reached
        raise AssertionError("no auth expected")

    def invalidate(self) -> None:  # pragma: no cover - never reached
        pass


def test_stalled_stream_is_dropped_and_resumed_with_range(
    stalling_server: _Server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(ca_bundle=_CERT, stall_timeout=0.5)
    governor = RequestGovernor(requests_per_second=1000.0, burst=1000)
    catalog = CddCatalog(settings=settings, governor=governor)
    auth = AuthManager(settings=settings, provider=_NoAuthProvider(), store=None)
    downloader = Downloader(
        catalog,
        auth,
        settings,
        governor=governor,
        retry_sleep=lambda _seconds: None,
        # Small chunks so the 400 bytes sent before the stall reach the
        # .part file (a read blocks until a whole chunk or EOF arrives).
        chunk_size=100,
    )
    url = f"https://localhost:{stalling_server.server_address[1]}/tile.tif"
    monkeypatch.setattr(downloader, "_get_presigned_url", lambda asset: url)

    asset = AssetRef(
        item_id="MDT-50cm-170122-06-2024",
        collection_id="MDT-50cm",
        asset_key="data",
        href="UNUSED",
        size_bytes=_SIZE,
        media_type="image/tiff",
        tile_key=None,
        geometry=Point(0, 0),
    )
    plan = downloader.plan([asset], tmp_path, FlatLayout())

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="cddpt.download"):
        outcomes = downloader.run(plan)
    elapsed = time.monotonic() - started

    assert outcomes[0].status == DownloadStatus.downloaded, outcomes[0].error
    assert outcomes[0].dest.read_bytes() == _BODY
    assert _StallingHandler.range_requests == [f"bytes={_FIRST_CHUNK}-"]
    # Detected after ~stall_timeout, not after the server's 5 s silence (or
    # the 120 s read_timeout).
    assert elapsed < _SERVER_STALL_SECONDS - 1
    assert any("resuming from byte 400" in record.getMessage() for record in caplog.records), (
        caplog.text
    )
