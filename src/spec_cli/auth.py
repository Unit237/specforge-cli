"""
CLI sign-in via Spec's device-flow broker.

Why this isn't Google directly anymore: the device flow grant is a
public-client OAuth grant that requires a registered client ID. Shipping
a Spec-owned Google client into every CLI binary means we own a public
secret forever (the client ID is leaked the moment we publish), can't
revoke individual sessions without churning the whole client, and have
to re-implement the wheel for every other identity provider we add.

So we don't. The CLI talks to **us**. Spec mints both halves of the
device flow (long random `device_code`, short human-friendly
`user_code`), tells the user to visit `https://spec.lightreach.io/device`,
and the web app — which already has a working Google sign-in — links
the code to the signed-in user. The CLI polls our token endpoint and
gets back a Spec session JWT. No Google OAuth client ID lives in this
binary.

Wire format mirrors RFC 8628 because that's already what `requests`-based
clients expect. Errors come back as ``{"error": "<code>"}`` with codes
``authorization_pending`` / ``slow_down`` / ``expired_token`` /
``access_denied``, identical to Google's.
"""

from __future__ import annotations

import time
import webbrowser
from dataclasses import dataclass

import requests


class AuthError(RuntimeError):
    pass


@dataclass
class DeviceCode:
    """The pending device-flow handshake.

    `verification_url` always points at the Spec deployment the CLI is
    talking to — `--api`/`SPEC_API`/the saved credential's `api_base`,
    in that order. We never embed a hardcoded URL here; the server
    decides where the user types the code.
    """

    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


@dataclass
class TokenBundle:
    """What `poll_for_token` returns once the user has approved.

    `user_email` / `user_name` / `user_handle` come back in the same
    response so the CLI can persist the credential file in one round
    trip — no second `/auth/me` call.
    """

    access_token: str
    user_email: str | None
    user_name: str | None
    user_handle: str | None


def request_device_code(api_base: str) -> DeviceCode:
    """Open a new device-flow handshake against ``api_base``."""
    url = api_base.rstrip("/") + "/api/auth/device/code"
    try:
        r = requests.post(url, json={}, timeout=30)
    except requests.RequestException as e:
        raise AuthError(f"Could not reach Spec at {api_base}: {e}") from e

    if r.status_code >= 400:
        raise AuthError(
            f"Spec device-code request failed: {r.status_code} {r.text}"
        )
    data = r.json()
    return DeviceCode(
        device_code=data["device_code"],
        user_code=data["user_code"],
        verification_url=data.get("verification_uri") or data.get("verification_url"),
        expires_in=int(data.get("expires_in", 600)),
        interval=int(data.get("interval", 5)),
    )


def poll_for_token(
    api_base: str,
    code: DeviceCode,
    *,
    open_browser: bool = True,
    tick=lambda remaining: None,
) -> TokenBundle:
    """Block until the user approves (or the code expires)."""
    if open_browser and code.verification_url:
        try:
            webbrowser.open_new(code.verification_url)
        except Exception:
            # `webbrowser.open_new` is best-effort; SSH sessions, headless
            # CI, and locked-down workstations all fail it. The CLI
            # already prints the URL — the user can visit it manually.
            pass

    deadline = time.time() + code.expires_in
    interval = code.interval
    token_url = api_base.rstrip("/") + "/api/auth/device/token"

    while time.time() < deadline:
        time.sleep(interval)
        tick(int(deadline - time.time()))

        try:
            r = requests.post(
                token_url,
                json={"device_code": code.device_code},
                timeout=30,
            )
        except requests.RequestException as e:
            # Transient — could be flaky network, server restart. Don't
            # bail on the user; just keep polling until the device code
            # itself expires.
            tick(int(deadline - time.time()))
            continue

        data = r.json() if r.content else {}

        if r.status_code == 200:
            user = (data or {}).get("user") or {}
            return TokenBundle(
                access_token=data["access_token"],
                user_email=user.get("email"),
                user_name=user.get("name"),
                user_handle=user.get("handle"),
            )

        # Device-flow error envelope: ``{"detail": {"error": "<code>"}}``
        # (FastAPI nests our HTTPException detail under ``detail``).
        # Tolerate the un-nested shape too in case a future server
        # change flattens it.
        detail = data.get("detail") if isinstance(data, dict) else None
        if isinstance(detail, dict) and "error" in detail:
            err = detail.get("error")
        else:
            err = (data or {}).get("error")

        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err == "expired_token":
            raise AuthError("Login code expired. Run `spec login` again.")
        if err == "access_denied":
            raise AuthError("Login denied.")
        raise AuthError(f"Login failed: {err or r.status_code} {r.text}")

    raise AuthError("Login timed out. Run `spec login` again.")
