"""Tests for cddpt.ratelimit: the shared token bucket + circuit breaker.

Everything here runs against a fake clock: ``sleep()`` just advances the
fake clock's counter rather than actually blocking, so a test that
"pauses" for ten minutes still runs instantly.
"""

from __future__ import annotations

import threading

import pytest

from cddpt.ratelimit import (
    CircuitBreaker,
    GovernorPause,
    PauseReason,
    RequestGovernor,
    ResponseLike,
)
from cddpt.settings import Settings


class FakeClock:
    """A controllable clock: ``sleep()`` advances it instead of blocking."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self.sleep_calls: list[float] = []

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self._now += seconds

    def advance(self, seconds: float) -> None:
        self._now += seconds


class FakeResponse:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = headers or {}


def _as_response_like(response: FakeResponse) -> ResponseLike:
    return response  # structurally compatible


# --------------------------------------------------------------------------
# CircuitBreaker
# --------------------------------------------------------------------------


def test_breaker_trips_after_five_failures_within_window() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(clock=clock.now)

    for _ in range(4):
        breaker.record_failure()
        clock.advance(10)
    assert not breaker.is_open

    breaker.record_failure()  # 5th failure, still within 120s window
    assert breaker.is_open


def test_breaker_does_not_trip_when_failures_spread_over_more_than_window() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(clock=clock.now, window_seconds=120.0)

    for _ in range(5):
        breaker.record_failure()
        clock.advance(40)  # 5 failures spread over 160s > 120s window

    assert not breaker.is_open


def test_breaker_cooldown_is_600_seconds() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(clock=clock.now, cooldown_seconds=600.0)

    for _ in range(5):
        breaker.record_failure()

    assert breaker.is_open
    assert breaker.seconds_until_clear() == pytest.approx(600.0)

    clock.advance(599)
    assert breaker.is_open

    clock.advance(1)
    assert not breaker.is_open
    assert breaker.seconds_until_clear() == 0.0


def test_breaker_default_thresholds_match_policy() -> None:
    """5 failures / 120s window / 600s cooldown, per docs/PLAN.md."""

    clock = FakeClock()
    breaker = CircuitBreaker(clock=clock.now)

    for _ in range(4):
        breaker.record_failure()
    assert not breaker.is_open

    breaker.record_failure()
    assert breaker.is_open
    assert breaker.seconds_until_clear() == pytest.approx(600.0)


# --------------------------------------------------------------------------
# RequestGovernor: token bucket pacing
# --------------------------------------------------------------------------


def test_governor_allows_a_burst_of_four_instantly() -> None:
    clock = FakeClock()
    governor = RequestGovernor(clock=clock.now, sleep=clock.sleep)

    for _ in range(4):
        governor.acquire()

    assert clock.sleep_calls == []  # burst of 4 fits with no waiting


def test_governor_paces_the_fifth_request_to_roughly_half_a_second() -> None:
    clock = FakeClock()
    governor = RequestGovernor(clock=clock.now, sleep=clock.sleep)

    for _ in range(4):
        governor.acquire()

    start = clock.now()
    governor.acquire()  # 5th request: bucket is exhausted, must wait for a refill
    elapsed = clock.now() - start

    # Sustained rate is 2/s, so waiting for one more token takes ~0.5s.
    assert elapsed == pytest.approx(0.5, abs=0.05)


def test_governor_sustained_rate_over_many_requests_is_about_two_per_second() -> None:
    clock = FakeClock()
    governor = RequestGovernor(clock=clock.now, sleep=clock.sleep)

    n = 20
    start = clock.now()
    for _ in range(n):
        governor.acquire()
    elapsed = clock.now() - start

    # First 4 are "free" (burst), the remaining 16 cost ~0.5s each.
    expected = (n - 4) * 0.5
    assert elapsed == pytest.approx(expected, abs=0.1)


def test_governor_honours_a_custom_rate_and_burst() -> None:
    clock = FakeClock()
    governor = RequestGovernor(
        clock=clock.now, sleep=clock.sleep, requests_per_second=10.0, burst=7
    )

    assert governor.requests_per_second == 10.0
    assert governor.burst == 7

    for _ in range(7):
        governor.acquire()
    assert clock.sleep_calls == []  # the configured burst of 7 fits with no waiting

    start = clock.now()
    governor.acquire()
    elapsed = clock.now() - start
    assert elapsed == pytest.approx(0.1, abs=0.02)  # sustained rate is 10/s


def test_governor_defaults_match_policy() -> None:
    clock = FakeClock()
    governor = RequestGovernor(clock=clock.now, sleep=clock.sleep)

    assert governor.requests_per_second == 2.0
    assert governor.burst == 4


def test_from_settings_uses_settings_requests_per_second_and_burst() -> None:
    clock = FakeClock()
    settings = Settings(_env_file=None, requests_per_second=5.0, burst=3)

    governor = RequestGovernor.from_settings(settings, clock=clock.now, sleep=clock.sleep)

    assert governor.requests_per_second == 5.0
    assert governor.burst == 3

    for _ in range(3):
        governor.acquire()
    assert clock.sleep_calls == []

    start = clock.now()
    governor.acquire()
    elapsed = clock.now() - start
    assert elapsed == pytest.approx(0.2, abs=0.02)  # sustained rate is 5/s


def test_from_settings_defaults_match_plain_constructor_defaults() -> None:
    clock = FakeClock()
    settings = Settings(_env_file=None)

    governor = RequestGovernor.from_settings(settings, clock=clock.now, sleep=clock.sleep)

    assert governor.requests_per_second == 2.0
    assert governor.burst == 4


# --------------------------------------------------------------------------
# RequestGovernor: Retry-After
# --------------------------------------------------------------------------


def test_retry_after_seconds_form_delays_next_acquire() -> None:
    clock = FakeClock()
    governor = RequestGovernor(clock=clock.now, sleep=clock.sleep)

    governor.acquire()
    governor.observe(_as_response_like(FakeResponse(429, {"Retry-After": "30"})))

    start = clock.now()
    governor.acquire()
    elapsed = clock.now() - start

    assert elapsed >= 30.0


def test_retry_after_takes_precedence_even_when_bucket_has_tokens() -> None:
    clock = FakeClock()
    governor = RequestGovernor(clock=clock.now, sleep=clock.sleep)

    # Bucket starts full (burst=4); Retry-After must still gate the very
    # next acquire even though tokens are available.
    governor.observe(_as_response_like(FakeResponse(503, {"Retry-After": "5"})))

    start = clock.now()
    governor.acquire()
    elapsed = clock.now() - start

    assert elapsed >= 5.0


def test_retry_after_without_the_header_does_not_delay() -> None:
    clock = FakeClock()
    governor = RequestGovernor(clock=clock.now, sleep=clock.sleep)

    governor.observe(_as_response_like(FakeResponse(200)))

    start = clock.now()
    governor.acquire()
    elapsed = clock.now() - start

    assert elapsed == 0.0


# --------------------------------------------------------------------------
# RequestGovernor: circuit breaker integration
# --------------------------------------------------------------------------


def test_five_429s_trip_the_breaker_and_pause_all_new_requests() -> None:
    clock = FakeClock()
    governor = RequestGovernor(clock=clock.now, sleep=clock.sleep)

    for _ in range(5):
        governor.observe(_as_response_like(FakeResponse(429)))

    assert governor.breaker.is_open

    start = clock.now()
    governor.acquire()
    elapsed = clock.now() - start

    assert elapsed >= 600.0


def test_503_responses_also_count_towards_the_breaker() -> None:
    clock = FakeClock()
    governor = RequestGovernor(clock=clock.now, sleep=clock.sleep)

    for _ in range(5):
        governor.observe(_as_response_like(FakeResponse(503)))

    assert governor.breaker.is_open


def test_non_failure_responses_do_not_trip_the_breaker() -> None:
    clock = FakeClock()
    governor = RequestGovernor(clock=clock.now, sleep=clock.sleep)

    for _ in range(20):
        governor.observe(_as_response_like(FakeResponse(200)))

    assert not governor.breaker.is_open


def test_observe_raw_is_equivalent_to_observe_for_the_breaker() -> None:
    """``observe_raw`` is what cddpt.http's urllib3 Retry hook calls for each
    intermediate response urllib3 retries internally -- it must behave
    exactly like ``observe()`` for the breaker and Retry-After tracking."""

    clock = FakeClock()
    governor = RequestGovernor(clock=clock.now, sleep=clock.sleep)

    for _ in range(5):
        governor.observe_raw(429, {})

    assert governor.breaker.is_open


def test_observe_raw_honours_retry_after() -> None:
    clock = FakeClock()
    governor = RequestGovernor(clock=clock.now, sleep=clock.sleep)

    governor.observe_raw(503, {"Retry-After": "12"})

    start = clock.now()
    governor.acquire()
    elapsed = clock.now() - start

    assert elapsed >= 12.0


# --------------------------------------------------------------------------
# Thread safety
# --------------------------------------------------------------------------


def test_acquire_is_thread_safe_under_concurrent_callers() -> None:
    clock = FakeClock()
    governor = RequestGovernor(clock=clock.now, sleep=clock.sleep)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(10):
                governor.acquire()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert all(not t.is_alive() for t in threads)


# --------------------------------------------------------------------------
# Observability: pause listeners + run stats (long waits must never be silent)
# --------------------------------------------------------------------------


class _RecordingListener:
    def __init__(self) -> None:
        self.events: list[tuple[str, GovernorPause]] = []

    def on_pause(self, pause: GovernorPause) -> None:
        self.events.append(("pause", pause))

    def on_resume(self, pause: GovernorPause) -> None:
        self.events.append(("resume", pause))


def test_breaker_wait_is_reported_to_listeners_and_counted() -> None:
    clock = FakeClock()
    governor = RequestGovernor(clock=clock.now, sleep=clock.sleep)
    listener = _RecordingListener()
    governor.add_pause_listener(listener)

    for _ in range(5):
        governor.observe(_as_response_like(FakeResponse(429)))
    governor.acquire()

    assert [kind for kind, _ in listener.events] == ["pause", "resume"]
    pause = listener.events[0][1]
    assert listener.events[1][1] is pause
    assert pause.reason is PauseReason.circuit_breaker
    assert pause.seconds == pytest.approx(600.0)

    stats = governor.stats()
    assert stats.throttled_responses == 5
    assert stats.breaker_trips == 1
    assert stats.paused_seconds == pytest.approx(600.0)


def test_retry_after_wait_is_reported_with_its_reason() -> None:
    clock = FakeClock()
    governor = RequestGovernor(clock=clock.now, sleep=clock.sleep)
    listener = _RecordingListener()
    governor.add_pause_listener(listener)

    governor.observe(_as_response_like(FakeResponse(429, {"Retry-After": "42"})))
    governor.acquire()

    (kind, pause), _ = listener.events
    assert kind == "pause"
    assert pause.reason is PauseReason.retry_after
    assert pause.seconds == pytest.approx(42.0)
    assert governor.stats().paused_seconds == pytest.approx(42.0)


def test_token_bucket_pacing_is_not_reported_as_a_pause() -> None:
    clock = FakeClock()
    governor = RequestGovernor(clock=clock.now, sleep=clock.sleep)
    listener = _RecordingListener()
    governor.add_pause_listener(listener)

    for _ in range(10):
        governor.acquire()

    assert listener.events == []
    assert governor.stats().paused_seconds == 0.0


def test_overlapping_pauses_are_counted_once_in_paused_seconds() -> None:
    clock = FakeClock()
    governor = RequestGovernor(clock=clock.now, sleep=clock.sleep)

    with governor.pausing(10.0, PauseReason.retry_backoff):
        clock.advance(5.0)
        with governor.pausing(10.0, PauseReason.retry_backoff):
            clock.advance(10.0)
    clock.advance(100.0)  # not paused

    assert governor.stats().paused_seconds == pytest.approx(15.0)


def test_a_failing_listener_never_breaks_the_request_path() -> None:
    class Broken:
        def on_pause(self, pause: GovernorPause) -> None:
            raise RuntimeError("ui bug")

        def on_resume(self, pause: GovernorPause) -> None:
            raise RuntimeError("ui bug")

    clock = FakeClock()
    governor = RequestGovernor(clock=clock.now, sleep=clock.sleep)
    governor.add_pause_listener(Broken())
    governor.observe(_as_response_like(FakeResponse(503, {"Retry-After": "3"})))

    governor.acquire()  # must not raise

    governor.remove_pause_listener(Broken())  # unknown listener: no-op
