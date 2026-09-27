"""The single place cddpt decides TLS trust, retry policy, timeouts, and
rate governance for outgoing HTTP requests.

Nothing else in cddpt should construct a :class:`requests.Session` or touch
``verify=`` -- always go through :func:`make_session`.

Why ``truststore``, never ``verify=False``
--------------------------------------------
The community plugin this project replaces unconditionally disabled TLS
certificate verification. The actual root cause it was working around: three
DGT hosts migrated from Sectigo/GÉANT to the HARICA TLS RSA Root CA 2021 in
late 2025 -- a legitimate, newer root that can be missing from stale or
vendored trust stores (e.g. QGIS's bundled Python on Windows), not a broken
server. Every relevant host verifies cleanly with an up-to-date trust store.

So: cddpt defaults to the OS-native trust store via :mod:`truststore`, offers
an explicit ``ca_bundle`` escape hatch for corporate TLS-inspecting proxies,
and never disables verification anywhere (enforced by
``tests/test_no_verify_false_anywhere.py``).

Why retries need their own governor hook
-------------------------------------------
``urllib3``'s ``Retry`` runs its retry loop *inside* one call to
``HTTPAdapter.send()`` -- below ``GovernedSession.send()``, not above it. If
the shared :class:`~cddpt.ratelimit.RequestGovernor` were only consulted in
``GovernedSession.send()``, three things would go wrong: retried wire
attempts would never acquire a token (bypassing the rate limit entirely);
the intermediate 429/503 responses that *trigger* those retries would never
reach the circuit breaker, only the final one would; and once urllib3's
retry budget is exhausted on a status in ``status_forcelist``, it raises
``MaxRetryError`` (which ``requests`` re-raises as ``RetryError``) --
``GovernedSession.send()`` would never even get a response object to call
``observe()`` on, so a sustained 429/503 storm could never trip the breaker.

:class:`_GovernedRetry` closes that gap by hooking urllib3's own retry
loop directly (see its docstring for the mechanism); :func:`make_session`
raises :class:`~cddpt.errors.HttpError` when that retry budget is exhausted,
so callers see a clear cddpt exception instead of a raw ``RetryError``.

Why every connection gets its own SSL context
-----------------------------------------------
``truststore.SSLContext`` (0.10.x) is not safe to share between threads:
around each handshake it temporarily switches the *shared* context to
``CERT_NONE``/``check_hostname=False`` (letting OpenSSL complete the
handshake so the OS verifier can run afterwards), and only the *entry* of
that switch is locked -- the handshake, the restore and the OS verification
(which reads ``verify_mode``/``check_hostname`` off the same context) are
not. With two download threads handshaking at once, one thread's
verification can observe the other's temporary ``CERT_NONE`` and accept an
untrusted certificate without a hostname check (urllib3 then emits
``InsecureRequestWarning``), and interleaved save/restore can leave the
shared context stuck at ``CERT_NONE``. Reproduced against a local
self-signed server: with two threads sharing one session, roughly half the
connections were *accepted* (see ``tests/test_http_tls_concurrency.py``).

So :class:`_TruststoreAdapter` never lets two connections share a context:
its HTTPS pools build a fresh, private context (via the same factory,
:func:`_build_ssl_context`) for every new connection, and a connection is
only ever used by one thread at a time. As a second, independent line of
defence, those pools *refuse* (raise :class:`~cddpt.errors.TlsVerificationError`)
to send anything over a connection urllib3 does not report as verified --
where stock urllib3 would merely emit ``InsecureRequestWarning`` and carry on.
Certificate-verification failures are never retried (they do not fix
themselves with backoff); they fail fast with a pointer to ``ca_bundle``.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

import requests
import truststore
import urllib3.util
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import MaxRetryError
from urllib3.poolmanager import PoolManager
from urllib3.util.ssl_match_hostname import CertificateError as _Urllib3CertificateError

from .errors import HttpError, TlsVerificationError
from .ratelimit import PauseReason, RequestGovernor
from .settings import Settings

if TYPE_CHECKING:
    from types import TracebackType

    from urllib3._base_connection import BaseHTTPSConnection
    from urllib3.connectionpool import ConnectionPool
    from urllib3.response import BaseHTTPResponse

#: Builds one fresh, verifying SSL context (see the module docstring for why
#: contexts are never shared between connections).
SSLContextFactory = Callable[[], ssl.SSLContext]

#: Appended to every TLS-verification error message.
_CA_BUNDLE_HINT = (
    "cddpt never disables certificate verification. If you are behind a "
    "TLS-inspecting (corporate) proxy, pass its CA certificate with "
    "--ca-bundle PATH (or CDDPT_CA_BUNDLE=PATH); otherwise treat this as a "
    "possible interception attempt."
)

#: Exactly the policy from docs/PLAN.md, "Backoff & rate-limiting policy":
#: server-sent Retry-After always takes precedence when present
#: (``respect_retry_after_header=True``). ``backoff_max`` is a constructor
#: argument only in urllib3>=2, hence the ``urllib3>=2`` dependency pin.
#: Kept as a plain dict (rather than a constructed ``Retry``) because every
#: session needs its own :class:`_GovernedRetry` bound to its own governor --
#: see the module docstring.
_RETRY_KWARGS: dict[str, Any] = {
    "total": 5,
    "backoff_factor": 2.0,
    "backoff_max": 120,
    "status_forcelist": (429, 500, 502, 503, 504),
    "respect_retry_after_header": True,
    "allowed_methods": frozenset({"GET", "POST"}),
}


def _find_cert_verification_error(error: BaseException | None) -> BaseException | None:
    """The certificate-verification failure somewhere in ``error``'s chain
    (``__cause__``/``__context__``/exception args -- urllib3 wraps the
    original ``ssl`` error in its own ``SSLError(original)``), or ``None``."""

    stack: list[object] = [error]
    seen: set[int] = set()
    while stack:
        candidate = stack.pop()
        if not isinstance(candidate, BaseException) or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        if isinstance(candidate, (ssl.SSLCertVerificationError, _Urllib3CertificateError)):
            return candidate
        stack.extend((candidate.__cause__, candidate.__context__, *candidate.args))
    return None


def _tls_error_message(host: str | None, detail: object) -> str:
    where = f" for {host}" if host else ""
    return f"cddpt: TLS certificate verification failed{where}: {detail}. {_CA_BUNDLE_HINT}"


class _GovernedRetry(urllib3.util.Retry):
    """A :class:`urllib3.util.Retry` that reports every wire-level retry
    attempt back to a :class:`~cddpt.ratelimit.RequestGovernor`.

    The exact parameter values from ``_RETRY_KWARGS`` are unchanged; this
    subclass only adds hooks around urllib3's own retry loop (see the module
    docstring for *why* this is necessary):

    - :meth:`increment` is what urllib3 calls once per wire attempt that
      needs a retry decision -- including the attempt that turns out to
      exhaust the retry budget -- so this is exactly where each
      intermediate response is available to report to the governor, before
      urllib3 decides whether to raise ``MaxRetryError``. If it does,
      this re-raises as a :class:`~cddpt.errors.HttpError` instead, so a
      cddpt caller never sees a raw ``requests.exceptions.RetryError``.
    - :meth:`sleep` runs immediately before each actual retry attempt, so
      this acquires a governor token there. urllib3's own
      ``backoff_factor``/``backoff_max``/``Retry-After`` pacing still runs
      too (via ``super().sleep()``) -- the two are complementary, not
      redundant: the governor's wait additionally covers the circuit
      breaker's cooldown and the *global* (cross-request) Retry-After.
    - A certificate-verification failure is never retried: :meth:`increment`
      raises :class:`~cddpt.errors.TlsVerificationError` immediately (a bad
      certificate does not fix itself with backoff, and retrying it would
      make a misconfigured trust store take minutes to report).
    - :meth:`new` is overridden because urllib3 replaces the ``Retry``
      instance with a fresh, decremented copy on every :meth:`increment`
      call; without this override, the governor reference would be dropped
      after the first retry.
    """

    _governor: RequestGovernor | None = None

    def new(self, **kw: Any) -> _GovernedRetry:
        new_retry = super().new(**kw)
        assert isinstance(new_retry, _GovernedRetry)
        new_retry._governor = self._governor
        return new_retry

    def increment(
        self,
        method: str | None = None,
        url: str | None = None,
        response: BaseHTTPResponse | None = None,
        error: Exception | None = None,
        _pool: ConnectionPool | None = None,
        _stacktrace: TracebackType | None = None,
    ) -> _GovernedRetry:
        if response is not None and self._governor is not None:
            self._governor.observe_raw(response.status, response.headers)

        cert_error = _find_cert_verification_error(error)
        if cert_error is not None:
            host = getattr(_pool, "host", None)
            raise TlsVerificationError(_tls_error_message(host, cert_error), url=url) from error

        try:
            result = super().increment(
                method=method,
                url=url,
                response=response,
                error=error,
                _pool=_pool,
                _stacktrace=_stacktrace,
            )
        except MaxRetryError as exc:
            status = response.status if response is not None else None
            detail = f"last status {status}" if status is not None else str(error)
            message = f"cddpt: HTTP retries exhausted for {method} {url} ({detail})"
            raise HttpError(message, status=status, url=url) from exc

        assert isinstance(result, _GovernedRetry)
        return result

    def sleep(self, response: BaseHTTPResponse | None = None) -> None:
        # urllib3's own backoff / Retry-After wait first; the governor's
        # gate then only adds whatever breaker cooldown or token wait is
        # still outstanding (acquiring first would wait a Retry-After twice).
        if self._governor is None:
            super().sleep(response)
            return
        planned = self._planned_sleep_seconds(response)
        if planned > 0:
            status = response.status if response is not None else None
            with self._governor.pausing(planned, PauseReason.retry_backoff, status=status):
                super().sleep(response)
        else:
            super().sleep(response)
        self._governor.acquire()

    def _planned_sleep_seconds(self, response: BaseHTTPResponse | None) -> float:
        """How long :meth:`urllib3.util.Retry.sleep` is about to wait -- the
        same decision it makes (a truthy ``Retry-After`` wins, else the
        exponential backoff), only computed up front so the wait can be
        reported instead of passing silently."""

        if self.respect_retry_after_header and response is not None:
            retry_after = self.get_retry_after(response)
            if retry_after:
                return retry_after
        return self.get_backoff_time()


def _build_retry(governor: RequestGovernor) -> _GovernedRetry:
    """A fresh :class:`_GovernedRetry`, with the exact policy values from
    ``_RETRY_KWARGS``, bound to ``governor``."""

    retry = _GovernedRetry(**_RETRY_KWARGS)
    retry._governor = governor
    return retry


def _build_ssl_context(settings: Settings) -> ssl.SSLContext:
    """One fresh verifying SSL context of the kind ``make_session`` uses --
    :class:`_TruststoreAdapter` calls this once per *connection* (see the
    module docstring: a context is never shared between connections).

    Uses the OS-native trust store (via ``truststore``) unless the caller
    has set an explicit ``ca_bundle`` (for corporate MITM proxies), in which
    case a standard *verifying* context is built from that CA file. Either
    way, certificate verification and hostname checking stay on.
    """

    if settings.ca_bundle is not None:
        return ssl.create_default_context(cafile=str(settings.ca_bundle))
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class _VerifiedHTTPSConnectionPool(HTTPSConnectionPool):
    """An HTTPS pool that (1) gives every new connection its own, private
    SSL context and (2) refuses to use a connection that is not verified.

    Concrete subclasses (one per adapter, see :class:`_TruststoreAdapter`)
    set :attr:`ssl_context_factory`.
    """

    ssl_context_factory: ClassVar[SSLContextFactory]

    def _new_conn(self) -> BaseHTTPSConnection:
        conn = super()._new_conn()
        # Replaces the pool-wide context urllib3 would otherwise hand every
        # connection (and every thread) -- see the module docstring.
        conn.ssl_context = type(self).ssl_context_factory()
        return conn

    def _validate_conn(self, conn: Any) -> None:
        # Mirrors HTTPSConnectionPool._validate_conn, except that an
        # unverified connection is a hard error instead of an
        # InsecureRequestWarning. Stricter than urllib3 on purpose: a
        # verified *proxy* does not excuse an unverified target.
        HTTPConnectionPool._validate_conn(self, conn)
        if conn.is_closed:
            conn.connect()
        if not conn.is_verified:
            conn.close()
            raise TlsVerificationError(
                _tls_error_message(
                    self.host, "the connection was not verified (refusing to send the request)"
                ),
                url=f"https://{self.host}",
            )


class _TruststoreAdapter(requests.adapters.HTTPAdapter):
    """An :class:`~requests.adapters.HTTPAdapter` whose HTTPS pools (direct
    and proxied) are :class:`_VerifiedHTTPSConnectionPool` instances building their
    per-connection SSL contexts with ``ssl_context_factory``."""

    def __init__(self, ssl_context_factory: SSLContextFactory, *args: Any, **kwargs: Any) -> None:
        self._ssl_context_factory = ssl_context_factory
        self._pool_cls: type[_VerifiedHTTPSConnectionPool] = type(
            "_CddptHTTPSConnectionPool",
            (_VerifiedHTTPSConnectionPool,),
            {"ssl_context_factory": staticmethod(ssl_context_factory)},
        )
        super().__init__(*args, **kwargs)

    def _install_pool_class(self, manager: PoolManager) -> None:
        # A new dict: PoolManager's default is a module-level one shared by
        # every PoolManager in the process.
        manager.pool_classes_by_scheme = {
            **manager.pool_classes_by_scheme,
            "https": self._pool_cls,
        }

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        # Still passed pool-wide: requests would otherwise substitute its own
        # certifi-based default context. Each connection replaces it with a
        # fresh one of its own (see _VerifiedHTTPSConnectionPool._new_conn).
        kwargs["ssl_context"] = self._ssl_context_factory()
        super().init_poolmanager(*args, **kwargs)
        self._install_pool_class(self.poolmanager)

    def proxy_manager_for(self, *args: Any, **kwargs: Any) -> Any:
        kwargs["ssl_context"] = self._ssl_context_factory()
        manager = super().proxy_manager_for(*args, **kwargs)
        self._install_pool_class(manager)
        return manager


class GovernedSession(requests.Session):
    """A :class:`requests.Session` with fixed default timeouts and shared
    rate governance.

    Overrides :meth:`send` -- rather than :meth:`~requests.Session.request`
    -- so that every actual HTTP round trip it makes (the initial request
    *and* each redirect hop) acquires a governor token first and reports the
    response back afterwards. ``requests``' response *hooks* run after the
    request has already gone out, so acquisition must happen before
    ``send()`` delegates to the underlying adapter.
    """

    def __init__(
        self,
        *,
        governor: RequestGovernor,
        connect_timeout: float,
        read_timeout: float,
    ) -> None:
        super().__init__()
        self._governor = governor
        self._default_timeout: tuple[float, float] = (connect_timeout, read_timeout)

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self._default_timeout

        self._governor.acquire()
        response = super().send(request, **kwargs)
        self._governor.observe(response)
        return response


def make_session(settings: Settings, *, governor: RequestGovernor | None = None) -> GovernedSession:
    """Build the one kind of session cddpt ever makes real requests with.

    Parameters
    ----------
    settings:
        Source of TLS (``ca_bundle``), timeout, and User-Agent configuration.
    governor:
        The shared :class:`~cddpt.ratelimit.RequestGovernor` to gate
        requests through. Pass the *same* instance to every session cddpt
        creates (search and download alike): the rate-limiting policy is a
        single shared budget, not one per session.
    """

    resolved_governor = (
        governor if governor is not None else RequestGovernor.from_settings(settings)
    )

    def ssl_context_factory() -> ssl.SSLContext:
        return _build_ssl_context(settings)

    adapter = _TruststoreAdapter(ssl_context_factory, max_retries=_build_retry(resolved_governor))

    session = GovernedSession(
        governor=resolved_governor,
        connect_timeout=settings.connect_timeout,
        read_timeout=settings.read_timeout,
    )
    # HTTPS only: cddpt never talks to the CDD portal over plain HTTP, and an
    # un-mounted "http://" would silently fall back to requests' own default
    # adapter (no retry policy, no governance).
    session.mount("https://", adapter)
    session.headers["User-Agent"] = settings.user_agent
    return session


__all__ = ["GovernedSession", "make_session"]
