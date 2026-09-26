"""Shared rate limiting and circuit breaker for all cddpt HTTP traffic.

Implements docs/PLAN.md's "Backoff & rate-limiting policy" exactly:

- **Global token bucket**: one shared limiter instance, used by both the
  search session and the download session (they hit the same backend): 2
  requests/second sustained, burst capacity 4.
- **Circuit breaker on top of per-request retry**: a rolling count of
  429/503 responses *across the whole run* (not per file); if 5 such
  failures occur within any 2-minute window, all new requests (search and
  download alike) pause for a 10-minute cooldown before resuming.
- Server-sent ``Retry-After`` always takes precedence when present: it
  extends a *global* wait, applied on top of the token bucket and the
  breaker.

Time and sleep are both injectable (``clock`` / ``sleep`` callables) so
tests can simulate minutes of elapsed time instantly, and so the whole
governor is deterministic under a fake clock.

Token bucket implementation notes
----------------------------------
Built on ``pyrate-limiter`` 4.x (checked against the installed version at
implementation time: 4.5.0). In this version, :class:`pyrate_limiter.Rate`
takes a ``burst`` parameter directly::

    Rate(limit=2, interval=Duration.SECOND, burst=4)

``burst`` is only honoured by pyrate-limiter's constant-state algorithms
(``GCRA`` / its subclass ``TokenBucket``), not by the window algorithms
(``SlidingWindowLog``, the default, or ``FixedWindow``) -- so this module
pairs that ``Rate`` with the ``TokenBucket`` algorithm explicitly. The
result is a classic token bucket: refills at ``requests_per_second`` tokens
per second, holds up to ``burst``, so a client that has been idle can burst
up to that many requests before settling back to the steady sustained rate.
This is an exact, native expression of "N requests/second sustained, burst
capacity B" -- no approximation via multiple stacked rates was needed. The
default (``requests_per_second=2.0``, ``burst=4``, from
:class:`cddpt.settings.Settings`) is exactly docs/PLAN.md's policy; both are
configurable since they're already-conservative *starting points*, not a
ceiling DGT has published.

``Rate.limit`` must be an ``int``, but ``requests_per_second`` is a float
(e.g. a fractional rate like 0.5/s). Rather than rounding the rate itself
(which would silently change slow/fractional rates), :func:`_build_rate`
scales both the limit and the interval up by ``_RATE_PRECISION`` so the
configured rate is expressed exactly (to 3 decimal places).

This module drives ``pyrate_limiter``'s ``StateBucket`` + ``TokenBucket``
algorithm directly (``bucket.put()`` / ``bucket.waiting()``) rather than
going through ``pyrate_limiter.Limiter``. ``Limiter``'s own blocking-acquire
path calls the real ``time.sleep``/``time.monotonic`` internally (not
injectable), which would make tests that exercise waiting behaviour
genuinely slow. Talking to the bucket directly, with our own injected clock
passed straight into ``StateBucket(..., clock=...)``, keeps 100% of the
timing deterministic and instant under test while still using pyrate-limiter
for the actual bucket/algorithm bookkeeping.

Note: ``TokenBucket``/``GCRA`` is a *constant-state* algorithm (a handful of
numbers per key, no per-item log), which pyrate-limiter 4.x stores in a
``StateBucket`` -- not the log-based ``InMemoryBucket`` used by the window
algorithms (``SlidingWindowLog``/``FixedWindow``). ``InMemoryBucket`` assumes
a ``LogAlgorithm`` and raises ``AttributeError`` if handed a
``StateAlgorithm`` like ``TokenBucket``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol

from pyrate_limiter import AbstractClock, Duration, Rate, RateItem, StateBucket, TokenBucket

from .settings import Settings

logger = logging.getLogger(__name__)

#: Seconds since some epoch (need not be wall-clock; monotonic is fine).
ClockFn = Callable[[], float]
#: Sleep for approximately this many seconds.
SleepFn = Callable[[float], None]

_BUCKET_NAME = "cddpt"
#: See :func:`_build_rate`: scales a fractional requests-per-second rate to
#: an exact integer ``Rate.limit`` (1/1000 requests/second precision).
_RATE_PRECISION = 1000


class ResponseLike(Protocol):
    """The minimal shape :meth:`RequestGovernor.observe` needs from a response.

    ``requests.Response`` satisfies this structurally (its ``headers`` is a
    ``CaseInsensitiveDict[str]``, a ``Mapping[str, str]``).
    """

    status_code: int

    @property
    def headers(self) -> Mapping[str, str]:
        """Read-only so a covariant ``Mapping`` subtype (e.g.
        ``requests``' ``CaseInsensitiveDict``) satisfies the protocol."""


class _InjectedClock(AbstractClock):
    """Adapts a ``() -> float`` seconds clock to pyrate-limiter's millisecond,
    integer clock interface."""

    def __init__(self, clock: ClockFn) -> None:
        self._clock = clock

    def now(self) -> int:
        return int(self._clock() * 1000)


def _build_rate(requests_per_second: float, burst: int) -> Rate:
    """The bucket's single rate: ``requests_per_second`` sustained, ``burst``.

    See the module docstring for why this is an exact expression of the
    policy (for any positive float rate) rather than an approximation.
    """

    limit = max(1, round(requests_per_second * _RATE_PRECISION))
    interval_ms = Duration.SECOND * _RATE_PRECISION
    return Rate(limit=limit, interval=interval_ms, burst=burst)


def _parse_retry_after_seconds(value: str) -> float | None:
    """Parse an HTTP ``Retry-After`` header value into a delay in seconds.

    Supports both forms from RFC 9110: an integer number of seconds, or an
    HTTP-date. The HTTP-date form is inherently wall-clock based (it is a
    point in time the server named), so it is resolved against real wall
    time regardless of any injected clock; the delta-seconds form is
    clock-agnostic and is applied relative to whatever clock the governor
    uses.
    """

    value = value.strip()
    if not value:
        return None

    if value.isdigit():
        return float(value)

    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    delta = (parsed - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, delta)


class CircuitBreaker:
    """Rolling-window circuit breaker over 429/503 responses.

    Tracks failures (429/503 responses) across the whole run -- not per
    file, per collection, or per download. If ``failure_threshold`` such
    failures land within any ``window_seconds`` window, the breaker opens:
    :meth:`seconds_until_clear` reports a ``cooldown_seconds`` cooldown,
    during which :class:`RequestGovernor` pauses all new requests.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        window_seconds: float = 120.0,
        cooldown_seconds: float = 600.0,
        clock: ClockFn = time.monotonic,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._window_seconds = window_seconds
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._failures: deque[float] = deque()
        self._open_until: float = 0.0
        self._lock = threading.Lock()

    def record_failure(self) -> None:
        """Record one 429/503 response, possibly tripping the breaker."""

        with self._lock:
            now = self._clock()
            self._failures.append(now)
            self._prune(now)
            if len(self._failures) >= self._failure_threshold:
                self._open_until = now + self._cooldown_seconds
                self._failures.clear()
                logger.warning(
                    "cddpt circuit breaker tripped: %d failures (HTTP 429/503) "
                    "within %.0fs. Pausing ALL new requests (search and "
                    "download) for %.0fs before resuming.",
                    self._failure_threshold,
                    self._window_seconds,
                    self._cooldown_seconds,
                )

    def _prune(self, now: float) -> None:
        while self._failures and (now - self._failures[0]) > self._window_seconds:
            self._failures.popleft()

    def seconds_until_clear(self) -> float:
        """Seconds remaining before the breaker allows requests again (0 if closed)."""

        with self._lock:
            remaining = self._open_until - self._clock()
        return remaining if remaining > 0 else 0.0

    @property
    def is_open(self) -> bool:
        return self.seconds_until_clear() > 0


class RequestGovernor:
    """Combines the shared token bucket, the circuit breaker, and a global
    ``Retry-After`` wait into one thread-safe gate for outgoing requests.

    Usage: call :meth:`acquire` before sending a request (blocks as needed,
    using the injected ``sleep``), send the request, then call
    :meth:`observe` with the response so 429/503s feed the breaker and any
    ``Retry-After`` header extends the next global wait.
    """

    def __init__(
        self,
        *,
        requests_per_second: float = 2.0,
        burst: int = 4,
        breaker: CircuitBreaker | None = None,
        clock: ClockFn = time.monotonic,
        sleep: SleepFn = time.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._requests_per_second = requests_per_second
        self._burst = burst
        self._breaker = breaker if breaker is not None else CircuitBreaker(clock=clock)
        # Kept as a typed reference (rather than reading it back off the
        # bucket) since AbstractBucket.now() is itself untyped upstream.
        self._pyrate_clock = _InjectedClock(clock)
        self._bucket = StateBucket(
            [_build_rate(requests_per_second, burst)],
            algorithm=TokenBucket(),
            clock=self._pyrate_clock,
        )
        self._retry_after_until: float = 0.0
        self._lock = threading.Lock()

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        breaker: CircuitBreaker | None = None,
        clock: ClockFn = time.monotonic,
        sleep: SleepFn = time.sleep,
    ) -> RequestGovernor:
        """Build a governor honouring ``settings.requests_per_second``/``burst``.

        This is what :func:`cddpt.http.make_session` uses when it is not
        handed an explicit, already-shared governor.
        """

        return cls(
            requests_per_second=settings.requests_per_second,
            burst=settings.burst,
            breaker=breaker,
            clock=clock,
            sleep=sleep,
        )

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    @property
    def requests_per_second(self) -> float:
        return self._requests_per_second

    @property
    def burst(self) -> int:
        return self._burst

    def acquire(self) -> None:
        """Block (via the injected ``sleep``) until a request may proceed.

        Waits, in order: for the circuit breaker's cooldown to clear, for
        any outstanding global ``Retry-After`` wait to clear, then for a
        token bucket slot.
        """

        self._wait_for_gate()
        self._wait_for_token()

    def observe(self, response: ResponseLike) -> None:
        """Feed a completed (final) response's outcome back into the governor.

        Records 429/503 responses against the circuit breaker, and honours
        a server-sent ``Retry-After`` header by extending the next global
        wait -- this takes precedence over (is applied in addition to) the
        token bucket's own pacing.
        """

        self.observe_raw(response.status_code, response.headers)

    def observe_raw(self, status_code: int, headers: Mapping[str, str]) -> None:
        """Lower-level form of :meth:`observe`, for callers that only have a
        raw status code and headers rather than a full response object.

        In particular, :mod:`cddpt.http`'s urllib3 ``Retry`` hook uses this
        to report *every* intermediate 429/503 response urllib3 retries
        internally -- not just the final response ``GovernedSession.send``
        sees -- so a sustained failure storm trips the breaker even though
        each individual logical request's retries are handled below
        ``GovernedSession.send()``.
        """

        retry_after = headers.get("Retry-After")
        if retry_after:
            delay = _parse_retry_after_seconds(retry_after)
            if delay is not None:
                with self._lock:
                    self._retry_after_until = max(self._retry_after_until, self._clock() + delay)

        if status_code in (429, 503):
            self._breaker.record_failure()

    def _wait_for_gate(self) -> None:
        while True:
            with self._lock:
                wait = max(
                    self._breaker.seconds_until_clear(),
                    self._retry_after_until - self._clock(),
                )
            if wait <= 0:
                return
            logger.info("cddpt request governor: pausing %.1fs before next request.", wait)
            self._sleep(wait)

    def _wait_for_token(self) -> None:
        while True:
            with self._lock:
                item = RateItem(_BUCKET_NAME, self._pyrate_clock.now(), weight=1)
                put_result = self._bucket.put(item)
                assert isinstance(put_result, bool), "StateBucket.put() is synchronous here"
                if put_result:
                    return
                wait_result = self._bucket.waiting(item)
                assert isinstance(wait_result, int), "StateBucket.waiting() is synchronous here"
                wait_ms = wait_result
            wait_seconds = (wait_ms / 1000) if wait_ms > 0 else 0.01
            self._sleep(wait_seconds)


__all__ = ["CircuitBreaker", "ClockFn", "RequestGovernor", "ResponseLike", "SleepFn"]
