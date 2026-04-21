"""`spec login` — Google OAuth device flow → ~/.spec/credentials."""

from __future__ import annotations

import click

from ..auth import AuthError, fetch_userinfo, poll_for_token, request_device_code
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
    """Sign in to Spec Cloud with Google (device flow)."""
    api_base = api or default_api_base()

    try:
        code = request_device_code()
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
            token = poll_for_token(code, open_browser=not no_browser)
    except AuthError as e:
        fatal(str(e))
        return

    info = fetch_userinfo(token.access_token) or {}
    creds = Credentials(
        api_base=api_base,
        access_token=token.access_token,
        refresh_token=token.refresh_token,
        user_email=info.get("email"),
        user_name=info.get("name"),
    )
    path = save_credentials(creds)

    who = info.get("email") or "signed in"
    ok(f"Signed in as [bold]{who}[/]")
    dim(f"Credentials saved to {path}")


@click.command("logout")
def logout_cmd() -> None:
    """Forget the stored credentials on this machine."""
    if clear_credentials():
        ok("Signed out.")
    else:
        dim("Already signed out.")
