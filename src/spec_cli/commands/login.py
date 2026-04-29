"""`spec login` — Spec-brokered device flow → ~/.spec/credentials.

The CLI mints no Google OAuth client; the server does that. We just
ask Spec for a (device_code, user_code) pair, tell the user where to
go, and poll until they've approved. By the time `poll_for_token`
returns we already have the user's email + handle in hand and can
write the credential file in one shot.
"""

from __future__ import annotations

import click

from ..auth import AuthError, poll_for_token, request_device_code
from ..config import Credentials, clear_credentials, default_api_base, save_credentials
from ..ui import console, dim, fatal, ok, pointer


@click.command("login")
@click.option(
    "--api",
    default=None,
    help="Override Cloud API base (otherwise $SPEC_API or the default).",
)
@click.option(
    "--no-browser",
    is_flag=True,
    help="Don't try to open a browser — just print the URL and code.",
)
def login_cmd(api: str | None, no_browser: bool) -> None:
    """Sign in to Spec Cloud with the device flow."""
    api_base = api or default_api_base()

    try:
        code = request_device_code(api_base)
    except AuthError as e:
        fatal(str(e))
        return

    console.print()
    pointer("Visit  ", code.verification_url)
    pointer("Code   ", code.user_code)
    dim(f"Expires in {code.expires_in // 60} min. Leave this running.")
    console.print()

    try:
        with console.status("[sf.muted]Waiting for approval…[/]", spinner="dots"):
            token = poll_for_token(api_base, code, open_browser=not no_browser)
    except AuthError as e:
        fatal(str(e))
        return

    creds = Credentials(
        api_base=api_base,
        access_token=token.access_token,
        user_email=token.user_email,
        user_name=token.user_name,
        user_handle=token.user_handle,
    )
    path = save_credentials(creds)

    if token.user_handle:
        ok(f"Signed in as [bold]@{token.user_handle}[/] ({token.user_email or 'no email'})")
    else:
        # Account exists but the user hasn't picked a handle yet — they
        # finished the device-flow approval before completing the
        # onboarding screen. Still a successful login; just nudge them
        # at the next push to set one.
        who = token.user_email or "signed in"
        ok(f"Signed in as [bold]{who}[/]")
        dim("Pick a handle in the Spec web app before pushing.")
    dim(f"Credentials saved to {path}")


@click.command("logout")
def logout_cmd() -> None:
    """Forget the stored credentials on this machine."""
    if clear_credentials():
        ok("Signed out.")
    else:
        dim("Already signed out.")
