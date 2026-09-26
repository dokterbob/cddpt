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
"""

from __future__ import annotations

import ssl
from typing import TYPE_CHECKING, Any

import requests
import truststore
import urllib3.util
from urllib3.exceptions import MaxRetryError

from .errors import HttpError
from .ratelimit import RequestGovernor
from .settings import Settings

if TYPE_CHECKING:
    from types import TracebackType

    from urllib3.connectionpool import ConnectionPool
    from urllib3.response import BaseHTTPResponse

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
        super().sleep(response)
        if self._governor is not None:
            self._governor.acquire()


def _build_retry(governor: RequestGovernor) -> _GovernedRetry:
    """A fresh :class:`_GovernedRetry`, with the exact policy values from
    ``_RETRY_KWARGS``, bound to ``governor``."""

    retry = _GovernedRetry(**_RETRY_KWARGS)
    retry._governor = governor
    return retry


def _build_ssl_context(settings: Settings) -> ssl.SSLContext:
    """The verifying SSL context ``make_session`` mounts on ``https://``.

    Uses the OS-native trust store (via ``truststore``) unless the caller
    has set an explicit ``ca_bundle`` (for corporate MITM proxies), in which
    case a standard *verifying* context is built from that CA file. Either
    way, certificate verification and hostname checking stay on.
    """

    if settings.ca_bundle is not None:
        return ssl.create_default_context(cafile=str(settings.ca_bundle))
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class _TruststoreAdapter(requests.adapters.HTTPAdapter):
    """An :class:`~requests.adapters.HTTPAdapter` bound to one explicit
    :class:`ssl.SSLContext`, used for both direct and proxied connections."""

    def __init__(self, ssl_context: ssl.SSLContext, *args: Any, **kwargs: Any) -> None:
        self._ssl_context = ssl_context
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        kwargs["ssl_context"] = self._ssl_context
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args: Any, **kwargs: Any) -> Any:
        kwargs["ssl_context"] = self._ssl_context
        return super().proxy_manager_for(*args, **kwargs)


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

    ssl_context = _build_ssl_context(settings)
    adapter = _TruststoreAdapter(ssl_context, max_retries=_build_retry(resolved_governor))

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
