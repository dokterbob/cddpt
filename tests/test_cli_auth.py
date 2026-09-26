"""Tests for ``cddpt auth login|status|logout|doctor`` via
:class:`typer.testing.CliRunner`.

Uses the ``fake_keyring``/``no_keyring_backend`` fixtures (never the real OS
keyring) and ``responses`` for the handful of tests that exercise a real
login flow -- never vcrpy/network for anything authenticated. Every test
that needs deterministic "no ambient credentials" behaviour relies on the
autouse ``_isolate_cddpt_credential_env_vars`` fixture in conftest.py.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import responses
from pydantic import SecretStr
from typer.testing import CliRunner

from cddpt.auth.base import AuthSession
from cddpt.auth.store import CredentialStore
from cddpt.cli import _common
from cddpt.cli.app import app

FIXTURES = Path(__file__).parent / "fixtures" / "auth"

SITE_URL = "https://cdd.dgterritorio.gov.pt"
AUTH_URL = "https://auth.cdd.dgterritorio.gov.pt"
LOGIN_URL_RE = re.compile(r"^https://cdd\.dgterritorio\.gov\.pt/auth/login.*")
AUTHORIZE_URL_RE = re.compile(
    r"^https://auth\.cdd\.dgterritorio\.gov\.pt/realms/dgterritorio/protocol/openid-connect/auth.*"
)
AUTHENTICATE_URL_RE = re.compile(
    r"^https://auth\.cdd\.dgterritorio\.gov\.pt/realms/dgterritorio/login-actions/authenticate.*"
)
CALLBACK_URL = f"{SITE_URL}/auth/callback"
DOWNLOADS_URL = f"{SITE_URL}/dgt-fe/downloads"

#: A rate policy fast enough to never actually sleep during a test.
_FAST_ENV = {"CDDPT_REQUESTS_PER_SECOND": "1000", "CDDPT_BURST": "1000"}


@pytest.fixture(autouse=True)
def _reset_cli_state() -> None:
    _common.set_state(verbose=False, ca_bundle=None)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _register_successful_login_chain(cookie_value: str = "sentinel-value") -> None:
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
        body=(FIXTURES / "login_page.html").read_text(encoding="utf-8"),
        content_type="text/html; charset=utf-8",
    )
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


def _register_invalid_credentials_chain() -> None:
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
        body=(FIXTURES / "login_page.html").read_text(encoding="utf-8"),
        content_type="text/html; charset=utf-8",
    )
    responses.add(
        responses.POST,
        AUTHENTICATE_URL_RE,
        status=200,
        body=(FIXTURES / "invalid_credentials.html").read_text(encoding="utf-8"),
        content_type="text/html; charset=utf-8",
    )


# ---------------------------------------------------------------------------
# auth login
# ---------------------------------------------------------------------------


@responses.activate
def test_login_with_env_credentials_succeeds_and_persists(
    runner: CliRunner, fake_keyring: object
) -> None:
    _register_successful_login_chain()
    env = {**_FAST_ENV, "CDDPT_USERNAME": "alice@example.test", "CDDPT_PASSWORD": "irrelevant"}

    result = runner.invoke(app, ["auth", "login"], env=env)

    assert result.exit_code == _common.EXIT_OK, result.stderr
    assert "logged in" in result.stdout
    assert "session valid until" in result.stdout
    # Never the username/email, per docs/PLAN.md's CLI contract.
    assert "alice@example.test" not in result.stdout

    store = CredentialStore()
    assert store.get_username() == "alice@example.test"
    assert store.load_session() is not None


@responses.activate
def test_login_prompts_for_username_and_password(
    runner: CliRunner, fake_keyring: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register_successful_login_chain()
    monkeypatch.setattr("cddpt.cli.auth.getpass.getpass", lambda prompt="": "sentinel-password")

    result = runner.invoke(
        app, ["auth", "login"], input="typed-username@example.test\n", env=_FAST_ENV
    )

    assert result.exit_code == _common.EXIT_OK, result.stderr
    # The prompt echoes the typed username back, as any interactive CLI
    # prompt does -- but the SUCCESS line itself must never repeat it.
    success_line = next(line for line in result.stdout.splitlines() if "logged in" in line)
    assert "typed-username@example.test" not in success_line

    store = CredentialStore()
    assert store.get_username() == "typed-username@example.test"


@responses.activate
def test_login_no_save_does_not_persist_credentials(
    runner: CliRunner, fake_keyring: object
) -> None:
    _register_successful_login_chain()
    env = {**_FAST_ENV, "CDDPT_USERNAME": "alice@example.test", "CDDPT_PASSWORD": "irrelevant"}

    result = runner.invoke(app, ["auth", "login", "--no-save"], env=env)

    assert result.exit_code == _common.EXIT_OK, result.stderr
    store = CredentialStore()
    assert store.get_username() is None
    assert store.load_session() is None


@responses.activate
def test_login_cookie_flow(
    runner: CliRunner, fake_keyring: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "cddpt.cli.auth.getpass.getpass", lambda prompt="": "pasted-connect-sid-value"
    )

    result = runner.invoke(app, ["auth", "login", "--cookie"], env=_FAST_ENV)

    assert result.exit_code == _common.EXIT_OK, result.stderr
    assert "logged in" in result.stdout

    store = CredentialStore()
    stored_cookie = store.get_manual_cookie()
    assert stored_cookie is not None
    assert stored_cookie.get_secret_value() == "pasted-connect-sid-value"


@responses.activate
def test_login_cookie_flow_no_save(
    runner: CliRunner, fake_keyring: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "cddpt.cli.auth.getpass.getpass", lambda prompt="": "pasted-connect-sid-value"
    )

    result = runner.invoke(app, ["auth", "login", "--cookie", "--no-save"], env=_FAST_ENV)

    assert result.exit_code == _common.EXIT_OK, result.stderr
    store = CredentialStore()
    assert store.get_manual_cookie() is None


def test_login_empty_cookie_exits_with_auth_failure_code(
    runner: CliRunner, fake_keyring: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cddpt.cli.auth.getpass.getpass", lambda prompt="": "")

    result = runner.invoke(app, ["auth", "login", "--cookie"], env=_FAST_ENV)

    assert result.exit_code == _common.EXIT_AUTH_FAILURE
    assert "Error" in result.stderr


@responses.activate
def test_login_invalid_credentials_exits_with_auth_failure_code(
    runner: CliRunner, fake_keyring: object
) -> None:
    _register_invalid_credentials_chain()
    env = {
        **_FAST_ENV,
        "CDDPT_USERNAME": "cddpt-probe-invalid@example.invalid",
        "CDDPT_PASSWORD": "wrong",
    }

    result = runner.invoke(app, ["auth", "login"], env=env)

    assert result.exit_code == _common.EXIT_AUTH_FAILURE
    assert "login failed" in result.stderr


# ---------------------------------------------------------------------------
# auth status
# ---------------------------------------------------------------------------


def test_status_with_no_cached_session(runner: CliRunner, fake_keyring: object) -> None:
    result = runner.invoke(app, ["auth", "status"], env=_FAST_ENV)
    assert result.exit_code == _common.EXIT_OK, result.stderr
    assert "none cached" in result.stdout


def test_status_with_valid_session(runner: CliRunner, fake_keyring: object) -> None:
    store = CredentialStore()
    now = datetime.now(timezone.utc)
    store.save_session(
        AuthSession(
            cookies={"connect.sid": "x"},
            obtained_at=now,
            expires_at=now + timedelta(minutes=20),
            source="form",
        )
    )

    result = runner.invoke(app, ["auth", "status"], env=_FAST_ENV)

    assert result.exit_code == _common.EXIT_OK, result.stderr
    assert "valid" in result.stdout
    assert "form" in result.stdout


def test_status_with_expired_session(runner: CliRunner, fake_keyring: object) -> None:
    store = CredentialStore()
    now = datetime.now(timezone.utc)
    store.save_session(
        AuthSession(
            cookies={"connect.sid": "x"},
            obtained_at=now - timedelta(minutes=40),
            expires_at=now - timedelta(minutes=10),
            source="manual",
        )
    )

    result = runner.invoke(app, ["auth", "status"], env=_FAST_ENV)
    assert result.exit_code == _common.EXIT_OK, result.stderr
    assert "expired" in result.stdout


def test_status_never_prints_secret_cookie_value(runner: CliRunner, fake_keyring: object) -> None:
    store = CredentialStore()
    now = datetime.now(timezone.utc)
    store.save_session(
        AuthSession(
            cookies={"connect.sid": "sentinel-should-never-print"},
            obtained_at=now,
            expires_at=now + timedelta(minutes=20),
            source="form",
        )
    )

    result = runner.invoke(app, ["auth", "status"], env=_FAST_ENV)
    assert "sentinel-should-never-print" not in result.stdout


def test_status_reports_keyring_unavailable(runner: CliRunner, no_keyring_backend: object) -> None:
    result = runner.invoke(app, ["auth", "status"], env=_FAST_ENV)
    assert result.exit_code == _common.EXIT_AUTH_FAILURE
    assert "unavailable" in result.stdout or "Error" in result.stderr


# ---------------------------------------------------------------------------
# auth logout
# ---------------------------------------------------------------------------


def test_logout_purges_all_entries(runner: CliRunner, fake_keyring: object) -> None:
    store = CredentialStore()
    store.set_username("alice@example.test")
    store.set_password("alice@example.test", SecretStr("x"))
    store.set_manual_cookie(SecretStr("y"))

    result = runner.invoke(app, ["auth", "logout"], env=_FAST_ENV)

    assert result.exit_code == _common.EXIT_OK, result.stderr
    assert "logged out" in result.stdout
    assert store.get_username() is None


def test_logout_exits_auth_failure_on_broken_keyring(
    runner: CliRunner, no_keyring_backend: object
) -> None:
    result = runner.invoke(app, ["auth", "logout"], env=_FAST_ENV)
    assert result.exit_code == _common.EXIT_AUTH_FAILURE


# ---------------------------------------------------------------------------
# auth doctor
# ---------------------------------------------------------------------------


@responses.activate
def test_doctor_all_ok(runner: CliRunner, fake_keyring: object) -> None:
    CredentialStore().set_username("someone")
    CredentialStore().set_password("someone", SecretStr("x"))

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
        body=(FIXTURES / "login_page.html").read_text(encoding="utf-8"),
        content_type="text/html; charset=utf-8",
    )
    responses.add(
        responses.GET,
        re.compile(
            r"^https://auth\.cdd\.dgterritorio\.gov\.pt/realms/dgterritorio/protocol/"
            r"openid-connect/auth\?client_id=aai-oidc-dgt.*"
        ),
        status=400,
    )

    result = runner.invoke(app, ["auth", "doctor"], env=_FAST_ENV)

    assert result.exit_code == _common.EXIT_OK, result.stderr
    assert "OK" in result.stdout
    assert "FAIL" not in result.stdout


@responses.activate
def test_doctor_some_fail_exits_generic_error(runner: CliRunner, fake_keyring: object) -> None:
    responses.add(responses.GET, SITE_URL, status=200)
    responses.add(responses.GET, AUTH_URL, status=200)
    responses.add(responses.GET, LOGIN_URL_RE, status=200, body="<html>no form here</html>")
    responses.add(
        responses.GET,
        re.compile(r"^https://auth\.cdd\.dgterritorio\.gov\.pt/realms.*"),
        status=400,
    )

    result = runner.invoke(app, ["auth", "doctor"], env=_FAST_ENV)

    assert result.exit_code == _common.EXIT_ERROR
    assert "FAIL" in result.stdout
