"""``cddpt auth login|status|logout|doctor``.

Thin formatting/prompting layer over :mod:`cddpt.auth` -- no HTTP/keyring
logic lives here. Every command is wrapped in ``_common.handle_errors``,
which maps an :class:`~cddpt.errors.AuthError` (raised by login/status/
logout on a real failure) to exit code 3 -- see cli/_common.py.

``login`` only ever handles the long-lived credential (username +
password) -- the CDD session itself is never persisted (see
``cddpt.auth.base``'s module docstring): it's established lazily, in
memory, the first time a command actually needs it. A manually-pasted
``connect.sid`` cookie is therefore a per-run input, not something to
"log in" with ahead of time -- see ``CDDPT_SESSION_COOKIE`` / ``download
--cookie``.

Nothing here ever prints a username, password, or cookie value -- login
only reports "logged in (session valid until HH:MM)", and status reports
*sources* (keyring/env/explicit), never the values themselves.
"""

from __future__ import annotations

import getpass
from typing import Annotated

import typer
from pydantic import SecretStr
from rich.console import Console
from rich.table import Table

from ..auth.base import AuthManager
from ..auth.form_provider import KeycloakFormAuthProvider
from ..auth.probe import doctor as run_doctor
from ..auth.store import CredentialStore
from . import _common

app = typer.Typer(name="auth", help="Manage CDD authentication.", no_args_is_help=True)

_stdout = Console()


@app.command("login", help="Log in to CDD and store the account credentials.")
@_common.handle_errors
def login(
    username: Annotated[
        str | None,
        typer.Option(
            "--username",
            help="CDD username/email (prompted if omitted and CDDPT_USERNAME is unset). "
            "Never pass a password this way -- it is always prompted, never a CLI argument.",
        ),
    ] = None,
    no_save: Annotated[
        bool,
        typer.Option(
            "--no-save",
            help="Don't persist credentials in the system keyring -- only cache the "
            "session in memory for this process.",
        ),
    ] = False,
) -> None:
    settings = _common.build_settings()
    store: CredentialStore | None = None if no_save else CredentialStore()

    resolved_username = username if username is not None else settings.username
    if resolved_username is None:
        resolved_username = typer.prompt("CDD username/email")

    password = settings.password
    if password is None:
        entered_password = getpass.getpass("CDD password (input hidden): ")
        password = SecretStr(entered_password)

    if store is not None:
        store.set_username(resolved_username)
        store.set_password(resolved_username, password)

    provider = KeycloakFormAuthProvider(
        settings=settings, username=resolved_username, password=password, store=store
    )

    manager = AuthManager(settings=settings, provider=provider)
    session = manager.current()
    _stdout.print(f"[green]logged in[/green] (session valid until {session.expires_at:%H:%M} UTC)")


@app.command("status", help="Show the current auth configuration.")
@_common.handle_errors
def status() -> None:
    settings = _common.build_settings()
    store = CredentialStore()

    table = Table(show_header=False, title="cddpt auth status")
    table.add_column("Field", style="bold")
    table.add_column("Value")

    table.add_row("Session", "not persisted across runs -- established in memory, once per process")

    if settings.username is not None:
        table.add_row("Credentials", "source: env (CDDPT_USERNAME/CDDPT_PASSWORD)")
    elif settings.session_cookie is not None:
        table.add_row("Credentials", "source: env (CDDPT_SESSION_COOKIE)")
    else:
        stored_username = store.get_username()
        table.add_row(
            "Credentials",
            "source: keyring" if stored_username is not None else "none configured",
        )

    table.add_row("Keyring backend", store.backend_name())

    _stdout.print(table)


@app.command(
    "logout",
    help="Purge the stored CDD credentials (username, password) from the keyring.",
)
@_common.handle_errors
def logout() -> None:
    store = CredentialStore()
    store.clear_all()
    _stdout.print("[green]logged out[/green] (stored credentials purged from the keyring)")


@app.command(
    "doctor",
    help="Run auth capability checks (TLS, login page, credentials, keyring). ",
)
@_common.handle_errors
def doctor(
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help="Also perform one real login and validate the resulting session against a "
            "live download-token exchange. Spends one real login and one search; never "
            "downloads or follows the pre-signed URL.",
        ),
    ] = False,
) -> None:
    settings = _common.build_settings()
    report = run_doctor(settings, live=live)

    table = Table(title="cddpt auth doctor")
    table.add_column("Check")
    table.add_column("Result")
    table.add_column("Detail")
    for check in report.checks:
        result = "[green]OK[/green]" if check.ok else "[red]FAIL[/red]"
        table.add_row(check.name, result, check.detail)
    _stdout.print(table)

    if not report.all_ok:
        raise typer.Exit(code=_common.EXIT_ERROR)


__all__ = ["app"]
