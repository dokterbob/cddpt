"""Tests for cddpt.auth.probe.doctor() -- all offline via ``responses``."""

from __future__ import annotations

import re
from pathlib import Path

import requests
import responses
from pydantic import SecretStr

from cddpt.auth.probe import doctor
from cddpt.auth.store import CredentialStore
from cddpt.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures" / "auth"

SITE_URL = "https://cdd.dgterritorio.gov.pt"
AUTH_URL = "https://auth.cdd.dgterritorio.gov.pt"
LOGIN_URL_RE = re.compile(r"^https://cdd\.dgterritorio\.gov\.pt/auth/login.*")
AUTHORIZE_URL_RE = re.compile(
    r"^https://auth\.cdd\.dgterritorio\.gov\.pt/realms/dgterritorio/protocol/openid-connect/auth.*"
)


def _register_reachability_and_login_page(fixture_name: str = "login_page.html") -> None:
    responses.add(responses.GET, SITE_URL, status=200)
    responses.add(responses.GET, AUTH_URL, status=200)
    responses.add(
        responses.GET,
        LOGIN_URL_RE,
        status=302,
        headers={
            "Location": (
                f"{AUTH_URL}/realms/dgterritorio/protocol/openid-connect/auth"
                "?session_code=abc&execution=def&client_id=aai-oidc-dgt&tab_id=xyz&client_data=w"
            )
        },
    )
    responses.add(
        responses.GET,
        AUTHORIZE_URL_RE,
        status=200,
        body=(FIXTURES / fixture_name).read_text(encoding="utf-8"),
        content_type="text/html; charset=utf-8",
    )


def _register_oauth_regression(*, still_rejected: bool) -> None:
    # Anchored on "?client_id=aai-oidc-dgt" as the FIRST query param --
    # matches only doctor()'s own regression probe (whose params dict is
    # {client_id, response_type, redirect_uri, scope}, in that order), never
    # the real login flow's authorize redirect (whose query starts with
    # session_code=...). Without this precise anchor, both GETs land in the
    # same broad "…/openid-connect/auth?…" URL space and `responses` can
    # reuse the wrong queued mock once its distinct entries are exhausted.
    status = 400 if still_rejected else 200
    responses.add(
        responses.GET,
        re.compile(
            r"^https://auth\.cdd\.dgterritorio\.gov\.pt/realms/dgterritorio/protocol/"
            r"openid-connect/auth\?client_id=aai-oidc-dgt.*"
        ),
        status=status,
    )


def _check(report: object, name: str) -> object:
    for check in report.checks:  # type: ignore[attr-defined]
        if check.name == name:
            return check
    raise AssertionError(f"no check named {name!r} in report")


@responses.activate
def test_doctor_all_checks_pass_on_a_healthy_setup(fake_keyring: object) -> None:
    _register_reachability_and_login_page()
    # oauth_regression's own GET (no query) is already registered above by
    # AUTHORIZE_URL_RE; register the query-string variant explicitly too so
    # it doesn't reuse the login-page body.
    responses.add(
        responses.GET,
        re.compile(
            r"^https://auth\.cdd\.dgterritorio\.gov\.pt/realms/dgterritorio/protocol/openid-connect/auth\?client_id=aai-oidc-dgt.*"
        ),
        status=400,
    )

    store = CredentialStore()
    store.set_username("someone")
    store.set_password("someone", SecretStr("x"))

    settings = Settings(username=None, password=None, requests_per_second=1000, burst=1000)
    report = doctor(settings, store=store, live=False)

    for check in report.checks:
        assert check.ok, f"{check.name}: {check.detail}"
    assert report.all_ok is True


@responses.activate
def test_doctor_reports_unreachable_host() -> None:
    responses.add(responses.GET, SITE_URL, body=requests.exceptions.ConnectionError("boom"))
    responses.add(responses.GET, AUTH_URL, status=200)
    responses.add(responses.GET, LOGIN_URL_RE, status=200, body="<html></html>")
    responses.add(
        responses.GET,
        re.compile(r"^https://auth\.cdd\.dgterritorio\.gov\.pt/realms.*"),
        status=400,
    )

    report = doctor(
        Settings(username=None, password=None, requests_per_second=1000, burst=1000),
        store=CredentialStore(),
        live=False,
    )
    check = _check(report, "cdd_site_reachable")
    assert check.ok is False  # type: ignore[attr-defined]


_AUTHENTICATE_ACTION = (
    "https://auth.cdd.dgterritorio.gov.pt/realms/dgterritorio/login-actions/authenticate?x=1"
)
_LOGIN_PAGE_MISSING_CREDENTIAL_ID = f"""
<html><body>
<form id="kc-form-login" action="{_AUTHENTICATE_ACTION}" method="post">
  <input type="text" name="username" id="username" />
  <input type="password" name="password" id="password" />
</form>
</body></html>
"""


@responses.activate
def test_doctor_reports_login_page_missing_expected_fields() -> None:
    responses.add(responses.GET, SITE_URL, status=200)
    responses.add(responses.GET, AUTH_URL, status=200)
    responses.add(
        responses.GET,
        LOGIN_URL_RE,
        status=302,
        headers={"Location": f"{AUTH_URL}/realms/dgterritorio/protocol/openid-connect/auth?x=1"},
    )
    responses.add(
        responses.GET,
        AUTHORIZE_URL_RE,
        status=200,
        body=_LOGIN_PAGE_MISSING_CREDENTIAL_ID,
        content_type="text/html; charset=utf-8",
    )
    responses.add(
        responses.GET,
        re.compile(
            r"^https://auth\.cdd\.dgterritorio\.gov\.pt/realms/dgterritorio/protocol/openid-connect/auth\?client_id.*"
        ),
        status=400,
    )

    report = doctor(
        Settings(username=None, password=None, requests_per_second=1000, burst=1000),
        store=CredentialStore(),
        live=False,
    )
    login_check = _check(report, "login_page")
    assert login_check.ok is False  # type: ignore[attr-defined]
    assert "credentialId" in login_check.detail  # type: ignore[attr-defined]


@responses.activate
def test_doctor_reports_fallback_form_still_passes() -> None:
    """A form without id="kc-form-login" (but with all expected fields) is
    still a passing login_page check -- see parse_login_page()'s documented
    fallback."""

    responses.add(responses.GET, SITE_URL, status=200)
    responses.add(responses.GET, AUTH_URL, status=200)
    responses.add(
        responses.GET,
        LOGIN_URL_RE,
        status=302,
        headers={"Location": f"{AUTH_URL}/realms/dgterritorio/protocol/openid-connect/auth?x=1"},
    )
    responses.add(
        responses.GET,
        AUTHORIZE_URL_RE,
        status=200,
        body=(FIXTURES / "fallback_form.html").read_text(encoding="utf-8"),
        content_type="text/html; charset=utf-8",
    )
    responses.add(
        responses.GET,
        re.compile(
            r"^https://auth\.cdd\.dgterritorio\.gov\.pt/realms/dgterritorio/protocol/openid-connect/auth\?client_id.*"
        ),
        status=400,
    )

    report = doctor(
        Settings(username=None, password=None, requests_per_second=1000, burst=1000),
        store=CredentialStore(),
        live=False,
    )
    login_check = _check(report, "login_page")
    assert login_check.ok is True  # type: ignore[attr-defined]


@responses.activate
def test_doctor_reports_missing_login_form() -> None:
    responses.add(responses.GET, SITE_URL, status=200)
    responses.add(responses.GET, AUTH_URL, status=200)
    responses.add(
        responses.GET,
        LOGIN_URL_RE,
        status=302,
        headers={"Location": f"{AUTH_URL}/realms/dgterritorio/protocol/openid-connect/auth?x=1"},
    )
    responses.add(
        responses.GET,
        AUTHORIZE_URL_RE,
        status=200,
        body=(FIXTURES / "no_form.html").read_text(encoding="utf-8"),
        content_type="text/html; charset=utf-8",
    )
    responses.add(
        responses.GET,
        re.compile(
            r"^https://auth\.cdd\.dgterritorio\.gov\.pt/realms/dgterritorio/protocol/openid-connect/auth\?client_id.*"
        ),
        status=400,
    )

    report = doctor(
        Settings(username=None, password=None, requests_per_second=1000, burst=1000),
        store=CredentialStore(),
        live=False,
    )
    login_check = _check(report, "login_page")
    assert login_check.ok is False  # type: ignore[attr-defined]


@responses.activate
def test_doctor_credentials_check_reports_source() -> None:
    _register_reachability_and_login_page()
    responses.add(
        responses.GET,
        re.compile(
            r"^https://auth\.cdd\.dgterritorio\.gov\.pt/realms/dgterritorio/protocol/openid-connect/auth\?client_id.*"
        ),
        status=400,
    )

    explicit_settings = Settings(
        username="someone", password=SecretStr("x"), requests_per_second=1000, burst=1000
    )
    report = doctor(explicit_settings, store=CredentialStore(), live=False)
    credentials_check = _check(report, "credentials")
    assert credentials_check.ok is True  # type: ignore[attr-defined]
    assert "explicit" in credentials_check.detail  # type: ignore[attr-defined]


@responses.activate
def test_doctor_credentials_check_reports_none_configured() -> None:
    _register_reachability_and_login_page()
    responses.add(
        responses.GET,
        re.compile(
            r"^https://auth\.cdd\.dgterritorio\.gov\.pt/realms/dgterritorio/protocol/openid-connect/auth\?client_id.*"
        ),
        status=400,
    )

    report = doctor(
        Settings(username=None, password=None, requests_per_second=1000, burst=1000),
        store=CredentialStore(),
        live=False,
    )
    credentials_check = _check(report, "credentials")
    assert credentials_check.ok is False  # type: ignore[attr-defined]


@responses.activate
def test_doctor_oauth_regression_detects_still_rejected() -> None:
    _register_reachability_and_login_page()
    _register_oauth_regression(still_rejected=True)

    report = doctor(
        Settings(username=None, password=None, requests_per_second=1000, burst=1000),
        store=CredentialStore(),
        live=False,
    )
    check = _check(report, "oauth_regression")
    assert check.ok is True  # type: ignore[attr-defined]


@responses.activate
def test_doctor_oauth_regression_flags_when_no_longer_rejected() -> None:
    _register_reachability_and_login_page()
    _register_oauth_regression(still_rejected=False)

    report = doctor(
        Settings(username=None, password=None, requests_per_second=1000, burst=1000),
        store=CredentialStore(),
        live=False,
    )
    check = _check(report, "oauth_regression")
    assert check.ok is False  # type: ignore[attr-defined]
    assert "file an issue" in check.detail  # type: ignore[attr-defined]


def test_doctor_keyring_backend_check(fake_keyring: object) -> None:
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, SITE_URL, status=200)
        rsps.add(responses.GET, AUTH_URL, status=200)
        rsps.add(responses.GET, LOGIN_URL_RE, status=200, body="<html></html>")
        rsps.add(
            responses.GET,
            re.compile(r"^https://auth\.cdd\.dgterritorio\.gov\.pt/realms.*"),
            status=400,
        )
        report = doctor(
            Settings(username=None, password=None, requests_per_second=1000, burst=1000),
            store=CredentialStore(),
            live=False,
        )
    check = _check(report, "keyring_backend")
    assert "InMemoryKeyring" in check.detail  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# doctor(live=True): fully mocked login + search + download-token exchange,
# never touching the real network.
# ---------------------------------------------------------------------------

CALLBACK_URL = f"{SITE_URL}/auth/callback"
DOWNLOADS_URL = f"{SITE_URL}/dgt-fe/downloads"
COLLECTIONS_URL = f"{SITE_URL}/dgt-be/v1/collections"
SEARCH_URL = f"{SITE_URL}/dgt-be/v1/search"
DOWNLOAD_URL = f"{SITE_URL}/dgt-be/v1/download/sentinel-token"


def _register_full_live_chain(
    *, download_status: int = 302, redirect_host: str = "stor-002.a.acnca.pt"
) -> None:
    responses.add(responses.GET, SITE_URL, status=200)
    responses.add(responses.GET, AUTH_URL, status=200)
    responses.add(
        responses.GET,
        LOGIN_URL_RE,
        status=302,
        headers={
            "Location": (
                f"{AUTH_URL}/realms/dgterritorio/protocol/openid-connect/auth"
                "?session_code=abc&execution=def&client_id=aai-oidc-dgt&tab_id=xyz&client_data=w"
            )
        },
    )

    # The authorize endpoint is hit >1 time in the live=True path (once by
    # doctor()'s own login_page check, once for real by the live login
    # itself) -- a plain responses.add() gets consumed after a single match
    # once more than one registration overlaps the same URL space (see
    # _register_oauth_regression's comment above), so this uses a callback
    # that persists across any number of calls and discriminates by query
    # content instead: only the oauth-regression probe's URL carries
    # response_type=code.
    login_html = (FIXTURES / "login_page.html").read_text(encoding="utf-8")

    def _authorize_callback(request: requests.PreparedRequest) -> tuple[int, dict[str, str], str]:
        if "response_type=code" in (request.url or ""):
            return 400, {}, ""
        return 200, {"Content-Type": "text/html; charset=utf-8"}, login_html

    responses.add_callback(responses.GET, AUTHORIZE_URL_RE, callback=_authorize_callback)

    responses.add(
        responses.POST,
        re.compile(
            r"^https://auth\.cdd\.dgterritorio\.gov\.pt/realms/dgterritorio/login-actions/authenticate.*"
        ),
        status=302,
        headers={"Location": CALLBACK_URL},
    )
    responses.add(
        responses.GET,
        CALLBACK_URL,
        status=302,
        headers={
            "Location": DOWNLOADS_URL,
            "Set-Cookie": (
                "connect.sid=s%3Asentinel.sig; Path=/; HttpOnly; Domain=cdd.dgterritorio.gov.pt"
            ),
        },
    )
    responses.add(responses.GET, DOWNLOADS_URL, status=200, body="<html>welcome</html>")

    responses.add(
        responses.GET,
        COLLECTIONS_URL,
        json={
            "status": 200,
            "message": "OK",
            "data": {"collections": [{"id": "LAZ", "summaries": {"visibility": ["show"]}}]},
        },
    )
    responses.add(
        responses.POST,
        SEARCH_URL,
        json={
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "id": "LO-114202-07-2024",
                    "collection": "LAZ",
                    "assets": {"data": {"href": DOWNLOAD_URL, "roles": ["data"]}},
                }
            ],
        },
    )
    location = f"https://{redirect_host}:9000/bucket/object?X-Amz-Expires=3600"
    responses.add(
        responses.GET, DOWNLOAD_URL, status=download_status, headers={"Location": location}
    )


@responses.activate
def test_doctor_live_true_reports_successful_session_validation(fake_keyring: object) -> None:
    _register_full_live_chain()
    settings = Settings(
        username="someone", password=SecretStr("x"), requests_per_second=1000, burst=1000
    )
    report = doctor(settings, store=CredentialStore(), live=True)

    login_check = _check(report, "live_login")
    assert login_check.ok is True  # type: ignore[attr-defined]
    assert "valid until" in login_check.detail  # type: ignore[attr-defined]

    gate_check = _check(report, "live_download_gate")
    assert gate_check.ok is True, gate_check.detail  # type: ignore[attr-defined]


@responses.activate
def test_doctor_live_true_flags_unexpected_download_response(fake_keyring: object) -> None:
    # An authorized session + a spent token gives 403 JSON, not a redirect
    # to a non-cdd host -- the gate check must catch that as unexpected.
    _register_full_live_chain(download_status=403, redirect_host="cdd.dgterritorio.gov.pt")
    settings = Settings(
        username="someone", password=SecretStr("x"), requests_per_second=1000, burst=1000
    )
    report = doctor(settings, store=CredentialStore(), live=True)

    gate_check = _check(report, "live_download_gate")
    assert gate_check.ok is False  # type: ignore[attr-defined]
