"""Tests for cddpt.auth.form_provider.KeycloakFormAuthProvider.

Fully offline via the ``responses`` library, against synthetic Keycloak HTML
fixtures under ``tests/fixtures/auth/`` (see that directory's fixtures'
docstring-comments for provenance -- modeled on one real, anonymous GET of
DGT's actual login page, plus one deliberate wrong-password probe using a
bogus username, never the real account). Never vcrpy/cassettes here -- this
is exactly the kind of authenticated-flow testing docs/PLAN.md's
secrets-handling rules reserve for ``responses`` + synthetic fixtures.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
import responses
from pydantic import SecretStr

from cddpt.auth.base import SESSION_TTL, AuthManager, AuthSession
from cddpt.auth.form_provider import KeycloakFormAuthProvider, parse_login_page
from cddpt.auth.store import CredentialStore
from cddpt.errors import AuthError
from cddpt.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures" / "auth"

LOGIN_URL_RE = re.compile(r"^https://cdd\.dgterritorio\.gov\.pt/auth/login.*")
AUTHORIZE_URL_RE = re.compile(
    r"^https://auth\.cdd\.dgterritorio\.gov\.pt/realms/dgterritorio/protocol/openid-connect/auth.*"
)
AUTHENTICATE_URL_RE = re.compile(
    r"^https://auth\.cdd\.dgterritorio\.gov\.pt/realms/dgterritorio/login-actions/authenticate.*"
)
CALLBACK_URL = "https://cdd.dgterritorio.gov.pt/auth/callback"
DOWNLOADS_URL = "https://cdd.dgterritorio.gov.pt/dgt-fe/downloads"

_SENTINEL_PASSWORD = "S3ntinel-Sup3r-Secret-Value-9f8e7d"


def _register_get_login_redirect(*, extra_set_cookie: str | None = None) -> None:
    headers = {
        "Location": (
            "https://auth.cdd.dgterritorio.gov.pt/realms/dgterritorio/protocol/"
            "openid-connect/auth?session_code=abc&execution=def&client_id=aai-oidc-dgt"
            "&tab_id=xyz&client_data=w"
        )
    }
    responses.add(responses.GET, LOGIN_URL_RE, status=302, headers=headers)
    # Keycloak's authorize endpoint itself sets AUTH_SESSION_ID/KC_RESTART on
    # the auth.cdd.dgterritorio.gov.pt domain -- these must NEVER leak into
    # the resulting AuthSession, which is scoped to the cdd site only.
    kc_cookie = (
        extra_set_cookie
        or "AUTH_SESSION_ID=should-never-leak; Domain=auth.cdd.dgterritorio.gov.pt; Path=/"
    )
    responses.add(
        responses.GET,
        AUTHORIZE_URL_RE,
        status=200,
        body=(FIXTURES / "login_page.html").read_text(encoding="utf-8"),
        content_type="text/html; charset=utf-8",
        headers={"Set-Cookie": kc_cookie},
    )


def _register_login_page(fixture_name: str) -> None:
    html = (FIXTURES / fixture_name).read_text(encoding="utf-8")
    responses.add(
        responses.GET,
        AUTHORIZE_URL_RE,
        status=200,
        body=html,
        content_type="text/html; charset=utf-8",
    )


def _register_successful_post(cookie_value: str = "sentinel-connect-sid-value") -> None:
    responses.add(
        responses.POST, AUTHENTICATE_URL_RE, status=302, headers={"Location": CALLBACK_URL}
    )
    responses.add(
        responses.GET,
        CALLBACK_URL,
        status=302,
        headers={
            "Location": DOWNLOADS_URL,
            "Set-Cookie": (
                f"connect.sid=s%3A{cookie_value}.sig; Path=/; HttpOnly; "
                "Domain=cdd.dgterritorio.gov.pt"
            ),
        },
    )
    responses.add(responses.GET, DOWNLOADS_URL, status=200, body="<html>welcome</html>")


def _register_failed_post(fixture_name: str) -> None:
    html = (FIXTURES / fixture_name).read_text(encoding="utf-8")
    # Keycloak re-renders the SAME login page (HTTP 200), never a redirect,
    # on a failed login attempt.
    responses.add(
        responses.POST,
        AUTHENTICATE_URL_RE,
        status=200,
        body=html,
        content_type="text/html; charset=utf-8",
    )


def _provider(**kwargs: object) -> KeycloakFormAuthProvider:
    return KeycloakFormAuthProvider(settings=Settings(), **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Happy path, across markup variations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_name", ["login_page.html", "login_page_variant_whitespace.html"])
@responses.activate
def test_successful_login_across_markup_variants(fixture_name: str) -> None:
    _register_get_login_redirect()
    responses.replace(
        responses.GET,
        AUTHORIZE_URL_RE,
        status=200,
        body=(FIXTURES / fixture_name).read_text(encoding="utf-8"),
        content_type="text/html; charset=utf-8",
    )
    _register_successful_post("abc123")

    clock_value = []
    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    clock_value.append(now)

    provider = _provider(
        username="irrelevant-for-the-mock",
        password=SecretStr("irrelevant"),
        clock=lambda: clock_value[0],
    )
    session = provider.authenticate()

    assert isinstance(session, AuthSession)
    assert session.source == "form"
    assert session.cookies["connect.sid"] == "s%3Aabc123.sig"
    assert session.obtained_at == now
    assert session.expires_at == now + SESSION_TTL
    # Keycloak's own auth.cdd.dgterritorio.gov.pt-scoped cookie must not leak.
    assert "AUTH_SESSION_ID" not in session.cookies


@responses.activate
def test_direct_auth_login_entrypoint_also_completes() -> None:
    """docs/PLAN.md asks to verify that starting at /auth/login directly
    (rather than via a /download/{token} redirect) also completes -- this
    is exactly the flow KeycloakFormAuthProvider always uses."""

    _register_get_login_redirect()
    _register_successful_post()
    provider = _provider(username="u", password=SecretStr("p"))
    session = provider.authenticate()
    assert "connect.sid" in session.cookies


# ---------------------------------------------------------------------------
# Session renewal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shortcut", ["site", "keycloak"])
@pytest.mark.parametrize("renewal", ["proactive", "unauthorized"])
@responses.activate
def test_renewal_starts_a_fresh_login(shortcut: str, renewal: str) -> None:
    """An existing CDD or SSO session skips the form without renewing its TTL."""
    _register_get_login_redirect()
    _register_successful_post("first")
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    provider = _provider(username="u", password=SecretStr("p"), clock=lambda: clock[0])
    manager = AuthManager(settings=Settings(), provider=provider, clock=lambda: clock[0])
    first = manager.current()

    authorize_url = next(
        call.request.url for call in responses.calls if AUTHORIZE_URL_RE.match(call.request.url)
    )
    responses.reset()

    def login(request: requests.PreparedRequest) -> tuple[int, dict[str, str], str]:
        if shortcut == "site" and "connect.sid=" in request.headers.get("Cookie", ""):
            return 302, {"Location": DOWNLOADS_URL}, ""
        return 302, {"Location": authorize_url}, ""

    def authorize(request: requests.PreparedRequest) -> tuple[int, dict[str, str], str]:
        if shortcut == "keycloak" and "AUTH_SESSION_ID=" in request.headers.get("Cookie", ""):
            return 302, {"Location": DOWNLOADS_URL}, ""
        return 200, {"Content-Type": "text/html"}, (FIXTURES / "login_page.html").read_text()

    responses.add_callback(responses.GET, LOGIN_URL_RE, callback=login)
    responses.add_callback(responses.GET, AUTHORIZE_URL_RE, callback=authorize)
    _register_successful_post("renewed")

    # Renew while the old server session is still valid, as in a long download.
    clock[0] += timedelta(minutes=27)
    second = manager.current() if renewal == "proactive" else manager.on_unauthorized(first)
    assert second.cookies["connect.sid"] == "s%3Arenewed.sig"
    assert second.cookies != first.cookies
    assert second.expires_at == clock[0] + SESSION_TTL
    assert manager.current() is second
    assert sum(call.request.method == "POST" for call in responses.calls) == 1


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


@responses.activate
def test_invalid_credentials_raises_with_keycloak_message() -> None:
    _register_get_login_redirect()
    _register_failed_post("invalid_credentials.html")

    provider = _provider(
        username="cddpt-probe-invalid@example.invalid", password=SecretStr("wrong")
    )
    with pytest.raises(AuthError) as excinfo:
        provider.authenticate()

    assert "Nome de utilizador ou palavra-passe inválida" in str(excinfo.value)


@responses.activate
def test_unexpected_required_action_page_names_it() -> None:
    _register_get_login_redirect()
    responses.replace(
        responses.GET,
        AUTHORIZE_URL_RE,
        status=200,
        body=(FIXTURES / "otp_required.html").read_text(encoding="utf-8"),
        content_type="text/html; charset=utf-8",
    )

    provider = _provider(username="u", password=SecretStr("p"))
    with pytest.raises(AuthError) as excinfo:
        provider.authenticate()

    message = str(excinfo.value)
    assert "one-time-password" in message
    assert "kc-otp-login-form" in message


@responses.activate
def test_missing_login_form_raises() -> None:
    _register_get_login_redirect()
    responses.replace(
        responses.GET,
        AUTHORIZE_URL_RE,
        status=200,
        body=(FIXTURES / "no_form.html").read_text(encoding="utf-8"),
        content_type="text/html; charset=utf-8",
    )

    provider = _provider(username="u", password=SecretStr("p"))
    with pytest.raises(AuthError, match="could not find a login form"):
        provider.authenticate()


@responses.activate
def test_fallback_form_used_with_warning_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="cddpt.auth.form_provider")
    _register_get_login_redirect()
    responses.replace(
        responses.GET,
        AUTHORIZE_URL_RE,
        status=200,
        body=(FIXTURES / "fallback_form.html").read_text(encoding="utf-8"),
        content_type="text/html; charset=utf-8",
    )
    _register_successful_post()

    provider = _provider(username="u", password=SecretStr("p"))
    session = provider.authenticate()

    assert "connect.sid" in session.cookies
    assert any("falling back" in record.getMessage() for record in caplog.records)


@responses.activate
def test_network_error_wraps_as_auth_error() -> None:
    responses.add(responses.GET, LOGIN_URL_RE, body=requests.exceptions.ConnectionError("boom"))
    provider = _provider(username="u", password=SecretStr("p"))
    with pytest.raises(AuthError, match="network error"):
        provider.authenticate()


# ---------------------------------------------------------------------------
# Credential resolution order: explicit > settings > keyring
# ---------------------------------------------------------------------------


def test_credentials_resolution_prefers_explicit_over_settings_and_store(
    fake_keyring: object,
) -> None:
    store = CredentialStore()
    store.set_username("from-store")
    store.set_password("from-store", SecretStr("store-password"))

    settings = Settings(username="from-settings", password=SecretStr("settings-password"))
    provider = KeycloakFormAuthProvider(
        settings=settings,
        username="from-explicit",
        password=SecretStr("explicit-password"),
        store=store,
    )
    username, password = provider._resolve_credentials()
    assert username == "from-explicit"
    assert password.get_secret_value() == "explicit-password"


def test_credentials_resolution_falls_back_to_settings() -> None:
    settings = Settings(username="from-settings", password=SecretStr("settings-password"))
    provider = KeycloakFormAuthProvider(settings=settings)
    username, password = provider._resolve_credentials()
    assert username == "from-settings"
    assert password.get_secret_value() == "settings-password"


def test_credentials_resolution_falls_back_to_store(fake_keyring: object) -> None:
    store = CredentialStore()
    store.set_username("from-store")
    store.set_password("from-store", SecretStr("store-password"))

    # Explicit None/None so this test is deterministic regardless of any
    # ambient CDDPT_USERNAME/CDDPT_PASSWORD in the real environment (see
    # settings.py: explicit constructor kwargs override env vars).
    provider = KeycloakFormAuthProvider(
        settings=Settings(username=None, password=None), store=store
    )
    username, password = provider._resolve_credentials()
    assert username == "from-store"
    assert password.get_secret_value() == "store-password"


def test_no_credentials_available_raises_and_is_available_reports_false() -> None:
    # Explicit None/None so this test is deterministic regardless of any
    # ambient CDDPT_USERNAME/CDDPT_PASSWORD in the real environment.
    provider = KeycloakFormAuthProvider(settings=Settings(username=None, password=None))
    assert provider.is_available() is False
    with pytest.raises(AuthError, match="no CDD credentials"):
        provider.authenticate()


# ---------------------------------------------------------------------------
# Secret hygiene: a sentinel password must never leak into logs/exceptions
# ---------------------------------------------------------------------------


@responses.activate
def test_password_sentinel_never_leaks_on_success(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    _register_get_login_redirect()
    _register_successful_post()

    provider = _provider(username="someone", password=SecretStr(_SENTINEL_PASSWORD))
    session = provider.authenticate()

    assert _SENTINEL_PASSWORD not in repr(session)
    assert _SENTINEL_PASSWORD not in str(session)
    for record in caplog.records:
        assert _SENTINEL_PASSWORD not in record.getMessage()

    settings_with_secret = Settings(username="someone", password=SecretStr(_SENTINEL_PASSWORD))
    assert _SENTINEL_PASSWORD not in repr(settings_with_secret)
    assert _SENTINEL_PASSWORD not in str(settings_with_secret)


@responses.activate
def test_password_sentinel_never_leaks_on_failure(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    _register_get_login_redirect()
    _register_failed_post("invalid_credentials.html")

    provider = _provider(
        username="cddpt-probe-invalid@example.invalid", password=SecretStr(_SENTINEL_PASSWORD)
    )
    with pytest.raises(AuthError) as excinfo:
        provider.authenticate()

    assert _SENTINEL_PASSWORD not in str(excinfo.value)
    assert _SENTINEL_PASSWORD not in repr(excinfo.value)
    for record in caplog.records:
        assert _SENTINEL_PASSWORD not in record.getMessage()


# ---------------------------------------------------------------------------
# parse_login_page(): shared with cddpt.auth.probe
# ---------------------------------------------------------------------------


def test_parse_login_page_extracts_expected_fields() -> None:
    html = (FIXTURES / "login_page.html").read_text(encoding="utf-8")
    parsed = parse_login_page(html, "https://auth.cdd.dgterritorio.gov.pt/realms/dgterritorio/x")
    assert {"username", "password", "credentialId"} <= parsed.field_names
    assert parsed.hidden_fields["credentialId"] == ""
    assert parsed.action_url.startswith(
        "https://auth.cdd.dgterritorio.gov.pt/realms/dgterritorio/login-actions/authenticate"
    )


def test_parse_login_page_raises_for_otp_page() -> None:
    html = (FIXTURES / "otp_required.html").read_text(encoding="utf-8")
    with pytest.raises(AuthError, match="one-time-password"):
        parse_login_page(html, "https://auth.cdd.dgterritorio.gov.pt/realms/dgterritorio/x")
