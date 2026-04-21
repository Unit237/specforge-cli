"""
Reads/writes the two config surfaces the CLI owns:

  1. `spec.yaml` at the bundle root — the manifest the compiler and the
     cloud both consume. We only load/dump it here; validation lives in the
     compiler (source of truth for the schema).

  2. `~/.spec/credentials` — a JSON file holding the Google OAuth refresh
     token and the Cloud API base URL. 0600 perms.

Everything path-related is resolved from the bundle root, which we find by
walking up from cwd looking for a `spec.yaml`.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .constants import MANIFEST_FILENAME


class BundleNotFoundError(FileNotFoundError):
    """Raised when no `spec.yaml` is found in cwd or any parent."""


@dataclass
class Manifest:
    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str | None:
        return self.data.get("name")

    @property
    def cloud_project(self) -> str | None:
        cloud = self.data.get("cloud") or {}
        return cloud.get("project")

    @property
    def root(self) -> Path:
        return self.path.parent


def find_bundle_root(start: Path | None = None) -> Path:
    """Walk up from `start` (default cwd) until we find a `spec.yaml`."""
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / MANIFEST_FILENAME).is_file():
            return candidate
    raise BundleNotFoundError(
        f"No {MANIFEST_FILENAME} found in {here} or any parent. "
        "Run `spec init` to scaffold one."
    )


def load_manifest(root: Path | None = None) -> Manifest:
    root = root or find_bundle_root()
    path = root / MANIFEST_FILENAME
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a YAML mapping at the top level")
    return Manifest(path=path, data=data)


def dump_manifest(manifest: Manifest) -> None:
    with manifest.path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            manifest.data,
            f,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )


# ---------------------------------------------------------------------------
# Credentials (~/.spec/credentials)
# ---------------------------------------------------------------------------


def _creds_dir() -> Path:
    override = os.environ.get("SPEC_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".spec"


def _creds_path() -> Path:
    return _creds_dir() / "credentials"


def default_api_base() -> str:
    return os.environ.get("SPEC_API", "https://spec.lightreach.io")


@dataclass
class Credentials:
    api_base: str
    access_token: str | None = None
    refresh_token: str | None = None
    user_email: str | None = None
    user_name: str | None = None


def load_credentials() -> Credentials | None:
    path = _creds_path()
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return Credentials(
        api_base=raw.get("api_base") or default_api_base(),
        access_token=raw.get("access_token"),
        refresh_token=raw.get("refresh_token"),
        user_email=raw.get("user_email"),
        user_name=raw.get("user_name"),
    )


def save_credentials(creds: Credentials) -> Path:
    d = _creds_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, stat.S_IRWXU)  # 0700
    except OSError:
        pass

    path = _creds_path()
    payload = {
        "api_base": creds.api_base,
        "access_token": creds.access_token,
        "refresh_token": creds.refresh_token,
        "user_email": creds.user_email,
        "user_name": creds.user_name,
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass
    return path


def clear_credentials() -> bool:
    path = _creds_path()
    if path.exists():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Remote URL parsing
# ---------------------------------------------------------------------------
#
# `spec push https://spec.lightreach.io/acme/billing.git` — git-style one-shot
# remotes. The URL carries both the Cloud host and the bundle slug so a user
# can push from a machine that has never seen `spec.yaml`'s `cloud:`
# block. Symmetrically used by `spec pull`.
#
# Rules, kept deliberately boring:
#   scheme     · http or https only (device-flow tokens never travel over
#                anything else)
#   host       · becomes the Cloud API base for this invocation (overrides
#                `SPEC_API` and the `api_base` in saved credentials)
#   path       · the full path is the slug, with any trailing `.git` stripped
#                and leading/trailing slashes removed. Multi-segment paths
#                (`acme/billing`) are preserved verbatim — forward-compatible
#                with namespacing without baking an owner rule into the CLI.
#   query/frag · rejected; they have no meaning here and a silent drop would
#                be a footgun.


class RemoteUrlError(ValueError):
    """The URL handed to push/pull can't be interpreted as a Cloud remote."""


@dataclass
class RemoteTarget:
    """Resolved push/pull target, explicit about which fields came from the URL."""

    api_base: str
    slug: str
    raw_url: str


def parse_remote_url(url: str) -> RemoteTarget:
    """
    Parse `https://host[/...]/slug(.git)?` into (api_base, slug).

    Raises RemoteUrlError with a user-facing message on any malformed input.
    """
    if not url or not isinstance(url, str):
        raise RemoteUrlError("Remote URL is empty.")

    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise RemoteUrlError(
            f"Remote URL must be http(s), got: {url!r}. "
            "Example: https://spec.lightreach.io/acme/billing.git"
        )
    if not parsed.netloc:
        raise RemoteUrlError(f"Remote URL is missing a host: {url!r}")
    if parsed.query or parsed.fragment:
        raise RemoteUrlError(
            f"Remote URL must not carry query or fragment: {url!r}"
        )

    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.strip("/")
    if not path:
        raise RemoteUrlError(
            f"Remote URL is missing a bundle slug: {url!r}. "
            "Example: https://spec.lightreach.io/acme/billing.git"
        )

    api_base = f"{parsed.scheme}://{parsed.netloc}"
    return RemoteTarget(api_base=api_base, slug=path, raw_url=url)
