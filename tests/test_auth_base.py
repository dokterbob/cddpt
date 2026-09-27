"""Tests for cddpt.auth.base: AuthSession, is_login_redirect, and
AuthManager's caching/expiry/proactive-refresh/thread-safety/cookie-scoping.

Fully offline -- no real HTTP, no real keyring backend needed: AuthManager
caches its session purely in memory and never touches a store of any kind.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
import requests

from cddpt.auth.base import SESSION_TTL, AuthManager, AuthSession, is_login_redirect
from cddpt.errors import AuthError, SessionExpired
from cddpt.settings import Settings

# ---------------------------------------------------------------------------
# AuthSession
# ---------------------------------------------------------------------------


def _session(
    *, obtained_at: datetime, ttl: timedelta = SESSION_TTL, source: str = "form"
) -> AuthSession:
    return AuthSession(
        cookies={"connect.sid": "sentinel-cookie-value"},
        obtained_at=obtained_at,
        expires_at=obtained_at + ttl,
        source=source,
    )


def test_is_expired_true_after_expiry() -> None:
    obtained = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session = _session(obtained_at=obtained)
    now = obtained + timedelta(minutes=31)
    assert session.is_expired(now=now) is True


def test_is_expired_false_before_expiry() -> None:
    obtained = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session = _session(obtained_at=obtained)
    now = obtained + timedelta(minutes=10)
    assert session.is_expired(now=now) is False


def test_is_expired_margin_triggers_early() -> None:
    obtained = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session = _session(obtained_at=obtained)
    now = obtained + timedelta(minutes=28)  # 2 min left
    assert session.is_expired(timedelta(minutes=3), now=now) is True
    assert session.is_expired(timedelta(minutes=1), now=now) is False


def test_auth_session_repr_never_contains_cookie_values() -> None:
    session = _session(obtained_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert "sentinel-cookie-value" not in repr(session)
    assert "sentinel-cookie-value" not in str(session)


# ---------------------------------------------------------------------------
# is_login_redirect
# ---------------------------------------------------------------------------


def _fake_response(
    status_code: int, *, location: str | None = None, history: list[requests.Response] | None = None
) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    if location is not None:
        response.headers["Location"] = location
    response.history = history or []
    response.url = "https://cdd.dgterritorio.gov.pt/dgt-be/v1/download/some-token"
    return response


def test_is_login_redirect_true_for_bare_302() -> None:
    response = _fake_response(302, location="https://cdd.dgterritorio.gov.pt/auth/login")
    assert is_login_redirect(response) is True


def test_is_login_redirect_true_for_relative_location() -> None:
    response = _fake_response(302, location="/auth/login")
    assert is_login_redirect(response) is True


def test_is_login_redirect_true_when_hop_is_in_history() -> None:
    hop = _fake_response(302, location="https://cdd.dgterritorio.gov.pt/auth/login")
    final = _fake_response(200, history=[hop])
    assert is_login_redirect(final) is True


def test_is_login_redirect_false_for_unrelated_redirect() -> None:
    response = _fake_response(302, location="https://cdd.dgterritorio.gov.pt/auth/callback")
    assert is_login_redirect(response) is False


def test_is_login_redirect_false_for_200() -> None:
    response = _fake_response(200)
    assert is_login_redirect(response) is False


def test_is_login_redirect_false_for_download_token_redirect_to_s3() -> None:
    # The "authorized session + fresh token" case from docs/PLAN.md's M4
    # pre-work: a 302 to a pre-signed S3 URL is NOT a login redirect.
    response = _fake_response(
        302, location="https://stor-002.a.acnca.pt:9000/bucket/object?X-Amz-Expires=3600"
    )
    assert is_login_redirect(response) is False


# ---------------------------------------------------------------------------
# AuthManager: fakes
# ---------------------------------------------------------------------------


class _FakeProvider:
    """A minimal AuthProvider double: hands out sessions with a fixed TTL,
    counting how many times each method was called (thread-safely)."""

    def __init__(self, *, clock: list[datetime], ttl: timedelta = SESSION_TTL) -> None:
        self._clock = clock
        self._ttl = ttl
        self.authenticate_calls = 0
        self.refresh_calls = 0
        self.invalidate_calls = 0
        self._lock = threading.Lock()
        self.fail_next_refresh: BaseException | None = None

    def is_available(self) -> bool:
        return True

    def authenticate(self) -> AuthSession:
        with self._lock:
            self.authenticate_calls += 1
        now = self._clock[0]
        return AuthSession(
            cookies={"connect.sid": f"session-{self.authenticate_calls}"},
            obtained_at=now,
            expires_at=now + self._ttl,
            source="form",
        )

    def refresh(self, session: AuthSession) -> AuthSession:
        with self._lock:
            self.refresh_calls += 1
            if self.fail_next_refresh is not None:
                exc, self.fail_next_refresh = self.fail_next_refresh, None
                raise exc
        now = self._clock[0]
        return AuthSession(
            cookies={"connect.sid": f"refreshed-{self.refresh_calls}"},
            obtained_at=now,
            expires_at=now + self._ttl,
            source="form",
        )

    def invalidate(self) -> None:
        with self._lock:
            self.invalidate_calls += 1


def _manager(*, clock: list[datetime], provider: _FakeProvider) -> AuthManager:
    return AuthManager(
        settings=Settings(),
        provider=provider,  # type: ignore[arg-type]
        clock=lambda: clock[0],
        refresh_margin=timedelta(minutes=3),
    )


# ---------------------------------------------------------------------------
# AuthManager.current(): caching / proactive refresh
# ---------------------------------------------------------------------------


def test_current_cold_start_authenticates_once() -> None:
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    provider = _FakeProvider(clock=clock)
    manager = _manager(clock=clock, provider=provider)

    session = manager.current()
    assert provider.authenticate_calls == 1
    assert session.cookies["connect.sid"] == "session-1"


def test_current_returns_cached_session_without_reauth() -> None:
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    provider = _FakeProvider(clock=clock)
    manager = _manager(clock=clock, provider=provider)

    first = manager.current()
    clock[0] += timedelta(minutes=5)
    second = manager.current()

    assert first is second
    assert provider.authenticate_calls == 1
    assert provider.refresh_calls == 0


def test_current_proactively_refreshes_within_margin() -> None:
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    provider = _FakeProvider(clock=clock)
    manager = _manager(clock=clock, provider=provider)

    manager.current()
    clock[0] += SESSION_TTL - timedelta(minutes=2)  # 2 min left, margin is 3
    second = manager.current()

    assert provider.authenticate_calls == 1
    assert provider.refresh_calls == 1
    assert second.cookies["connect.sid"] == "refreshed-1"


# ---------------------------------------------------------------------------
# AuthManager.on_unauthorized(): manual-provider-style hard failure
# ---------------------------------------------------------------------------


def test_on_unauthorized_calls_refresh_not_authenticate() -> None:
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    provider = _FakeProvider(clock=clock)
    manager = _manager(clock=clock, provider=provider)

    first = manager.current()
    second = manager.on_unauthorized(first)

    assert provider.authenticate_calls == 1
    assert provider.refresh_calls == 1
    assert provider.invalidate_calls == 1
    assert second.cookies["connect.sid"] == "refreshed-1"


def test_on_unauthorized_propagates_session_expired_from_manual_refresh() -> None:
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    provider = _FakeProvider(clock=clock)
    provider.fail_next_refresh = SessionExpired("cddpt: paste a fresh cookie")
    manager = _manager(clock=clock, provider=provider)

    session = manager.current()
    with pytest.raises(SessionExpired):
        manager.on_unauthorized(session)


def test_on_unauthorized_no_op_if_another_thread_already_refreshed() -> None:
    """If the cached session has already moved on from `failed_session` by
    the time this call gets the lock, it must return the fresh one directly
    -- never re-authenticate a second time."""

    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    provider = _FakeProvider(clock=clock)
    manager = _manager(clock=clock, provider=provider)

    stale_session = manager.current()
    fresh_session = manager.on_unauthorized(stale_session)
    assert provider.refresh_calls == 1

    # A second caller reporting the SAME (now stale) session must not
    # trigger a second refresh.
    result = manager.on_unauthorized(stale_session)
    assert result is fresh_session
    assert provider.refresh_calls == 1


def test_on_unauthorized_concurrent_threads_trigger_exactly_one_reauth() -> None:
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    provider = _FakeProvider(clock=clock)
    manager = _manager(clock=clock, provider=provider)

    failed_session = manager.current()
    assert provider.authenticate_calls == 1

    results: list[AuthSession] = []
    results_lock = threading.Lock()

    def worker() -> None:
        result = manager.on_unauthorized(failed_session)
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert provider.refresh_calls == 1
    assert len({id(r) for r in results}) == 1


# ---------------------------------------------------------------------------
# AuthManager.apply(): cookie domain scoping
# ---------------------------------------------------------------------------


def test_apply_scopes_cookies_to_cdd_domain_only() -> None:
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    provider = _FakeProvider(clock=clock)
    manager = _manager(clock=clock, provider=provider)

    http_session = requests.Session()
    manager.apply(http_session)

    assert http_session.cookies.get("connect.sid", domain="cdd.dgterritorio.gov.pt") == "session-1"
    # Never sent to the pre-signed-URL host -- see docs/PLAN.md's M4
    # pre-work: "[the S3 URL] needs no cookies".
    assert http_session.cookies.get("connect.sid", domain="stor-002.a.acnca.pt") is None


def test_apply_raises_config_error_for_hostless_site_base_url() -> None:
    from cddpt.errors import ConfigError

    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    provider = _FakeProvider(clock=clock)
    manager = AuthManager(
        settings=Settings(site_base_url="not-a-url"),
        provider=provider,  # type: ignore[arg-type]
        clock=lambda: clock[0],
    )
    with pytest.raises(ConfigError):
        manager.apply(requests.Session())


def test_auth_error_is_hashable_free_of_secrets_in_message() -> None:
    """A basic guard: constructing an AuthError with a message never
    silently pulls in a __repr__ of a secret-carrying object."""

    exc = AuthError("cddpt: generic failure")
    assert str(exc) == "cddpt: generic failure"
