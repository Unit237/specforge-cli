"""
Google OAuth 2.0 Device Authorization Grant (RFC 8628).

We use device flow because the CLI has to work over SSH and in CI — no
web-redirect available. The user sees a short code + URL, confirms in a
browser on any device, and we poll Google's token endpoint until it hands
us a refresh token.

For v0.1 the Cloud OAuth client ID is embedded. When the real
Spec Cloud OAuth client is provisioned, swap the `DEFAULT_CLIENT_ID`
below (or override at runtime via `SPEC_OAUTH_CLIENT_ID`). The client
secret is not shipped — device flow is a public-client grant.
"""

from __future__ import annotations

import os
import time
import webbrowser
from dataclasses import dataclass

import requests

DEVICE_CODE_ENDPOINT = "https://oauth2.googleapis.com/device/code"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"

# Placeholder — replace when the real Cloud OAuth client is issued.
DEFAULT_CLIENT_ID = "spec-cli.apps.googleusercontent.com"

SCOPES = "openid email profile"
GRANT_TYPE_DEVICE = "urn:ietf:params:oauth:grant-type:device_code"


class AuthError(RuntimeError):
    pass


@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str | None
    id_token: str | None
    expires_in: int


def _client_id() -> str:
    return os.environ.get("SPEC_OAUTH_CLIENT_ID", DEFAULT_CLIENT_ID)


def request_device_code() -> DeviceCode:
    r = requests.post(
        DEVICE_CODE_ENDPOINT,
        data={"client_id": _client_id(), "scope": SCOPES},
        timeout=30,
    )
    if r.status_code >= 400:
        raise AuthError(f"Google device-code request failed: {r.status_code} {r.text}")
    data = r.json()
    return DeviceCode(
        device_code=data["device_code"],
        user_code=data["user_code"],
        verification_url=data.get("verification_url") or data.get("verification_uri"),
        expires_in=int(data.get("expires_in", 600)),
        interval=int(data.get("interval", 5)),
    )


def poll_for_token(
    code: DeviceCode,
    *,
    open_browser: bool = True,
    tick=lambda remaining: None,
) -> TokenBundle:
    if open_browser:
        try:
            webbrowser.open_new(code.verification_url)
        except Exception:
            pass

    deadline = time.time() + code.expires_in
    interval = code.interval

    while time.time() < deadline:
        time.sleep(interval)
        tick(int(deadline - time.time()))

        r = requests.post(
            TOKEN_ENDPOINT,
            data={
                "client_id": _client_id(),
                "device_code": code.device_code,
                "grant_type": GRANT_TYPE_DEVICE,
            },
            timeout=30,
        )
        data = r.json() if r.content else {}

        if r.status_code == 200:
            return TokenBundle(
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token"),
                id_token=data.get("id_token"),
                expires_in=int(data.get("expires_in", 3600)),
            )

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


def fetch_userinfo(access_token: str) -> dict:
    r = requests.get(
        USERINFO_ENDPOINT,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    if r.status_code >= 400:
        return {}
    return r.json()
