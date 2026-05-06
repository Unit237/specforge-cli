"""
Reads/writes the two config surfaces the CLI owns:

  1. `spec.yaml` at the bundle root — the manifest the compiler and the
     cloud both consume. We only load/dump it here; validation lives in the
     compiler (source of truth for the schema).

  2. `~/.spec/credentials` — a JSON file holding the Spec session token,
     the Cloud API base URL, and the signed-in user's public handle.
     0600 perms.

Everything path-related is resolved from the bundle root, which we find by
walking up from cwd looking for a `spec.yaml`.
"""

from __future__ import annotations

import json
import os
import re
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
    def cloud_bundle_id(self) -> str | None:
        """Stable, server-minted bundle identity (``cloud.bundle_id`` in
        ``spec.yaml``).

        This is the half of the manifest the user *cannot* sensibly
        edit — it's stamped by the CLI on the first successful push and
        verified on every subsequent push so a working tree bound to
        bundle A can never be retargeted to bundle B by editing
        ``cloud.project``. ``None`` when missing — older manifests
        won't carry it, and the first-push adoption flow is what fills
        it in. See ``ensure_bundle_id_binding`` for the verify/adopt
        logic and PLAN.md §11 for the design rationale.
        """
        cloud = self.data.get("cloud") or {}
        value = cloud.get("bundle_id")
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned or None

    def set_cloud_bundle_id(self, bundle_id: str) -> None:
        """Write ``cloud.bundle_id`` into the in-memory manifest.

        Caller is responsible for persisting via ``dump_manifest``. We
        keep the mutation explicit (no auto-save) so the push pipeline
        can decide *when* the on-disk file changes — the adopt-on-first-
        push flow only writes after the upload succeeds, so a failed
        push doesn't leave the manifest in a confusing half-bound state.
        """
        cloud = self.data.get("cloud")
        if not isinstance(cloud, dict):
            cloud = {}
            self.data["cloud"] = cloud
        cloud["bundle_id"] = bundle_id

    def set_cloud_project(self, project: str) -> None:
        """Write ``cloud.project`` (bare slug or ``<handle>/<slug>``)."""
        cloud = self.data.get("cloud")
        if not isinstance(cloud, dict):
            cloud = {}
            self.data["cloud"] = cloud
        cloud["project"] = project

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
    """What the CLI persists to ``~/.spec/credentials`` after login.

    ``access_token`` is the Spec session JWT (the same one the web app
    sends as ``Authorization: Bearer …``); ``user_handle`` is the public
    namespace prefix Cloud assigned this account, captured at login
    time so ``cloud.project: <slug>`` (without a handle) can fall back
    to it. The legacy ``refresh_token`` field stays in the dataclass
    for back-compat with previously-written credential files; we don't
    use it any more (the device-flow broker hands out a long-lived JWT
    instead of a refresh-token pair).
    """

    api_base: str
    access_token: str | None = None
    refresh_token: str | None = None
    user_email: str | None = None
    user_name: str | None = None
    user_handle: str | None = None


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
        user_handle=raw.get("user_handle"),
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
        "user_handle": creds.user_handle,
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
# `spec push https://spec.lightreach.io/<handle>/<slug>.git` — git-style
# one-shot remotes. The URL carries the Cloud host plus the *two-part*
# `<handle>/<slug>` identifier the server actually resolves on. Mirrors
# the GitHub URL shape exactly so muscle memory carries over.
#
# Rules, kept deliberately boring:
#   scheme     · http or https only (session tokens never travel over
#                anything else).
#   host       · becomes the Cloud API base for this invocation
#                (overrides `SPEC_API` and the `api_base` in saved
#                credentials).
#   path       · exactly two segments: ``<handle>/<slug>``. Trailing
#                ``.git`` stripped. More segments fail loudly so a
#                pasted nested URL routes somewhere obvious instead of
#                being silently flattened into a slug.
#   query/frag · rejected; they have no meaning here and a silent drop
#                would be a footgun.


class RemoteUrlError(ValueError):
    """The URL handed to push/pull can't be interpreted as a Cloud remote."""


# Handle + slug syntactic checks (kept intentionally lax — the server is
# the source of truth on availability, this is just "shape looks right").
_HANDLE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?$")
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,254}[a-z0-9])?$", re.IGNORECASE)


@dataclass
class RemoteTarget:
    """Resolved push/pull target, explicit about which fields came from the URL.

    ``handle`` and ``slug`` are split — they're the two segments after
    the host. ``raw_url`` keeps the original string for log lines /
    error messages so the user always sees what they typed back.
    """

    api_base: str
    handle: str
    slug: str
    raw_url: str


def parse_remote_url(url: str) -> RemoteTarget:
    """Parse ``https://host/<handle>/<slug>(.git)?`` into its parts.

    Raises ``RemoteUrlError`` with a user-facing message on any
    malformed input.
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
            f"Remote URL is missing the <handle>/<slug> path: {url!r}. "
            "Example: https://spec.lightreach.io/acme/billing.git"
        )

    parts = path.split("/")
    if len(parts) == 1:
        raise RemoteUrlError(
            f"Remote URL is missing a handle: {url!r}. "
            "URLs are now `<host>/<handle>/<slug>` — the GitHub shape. "
            "Example: https://spec.lightreach.io/acme/billing.git"
        )
    if len(parts) > 2:
        raise RemoteUrlError(
            f"Remote URL must be exactly `<host>/<handle>/<slug>`, got "
            f"{url!r}. If your handle has a `/` in it, that's not "
            "supported — handles are a single segment."
        )

    handle, slug = parts[0].lower(), parts[1]
    if not _HANDLE_RE.match(handle):
        raise RemoteUrlError(
            f"Handle `{handle}` doesn't look right. Use 1–39 lowercase "
            "letters, digits, and single hyphens (no leading/trailing hyphen)."
        )
    if not _SLUG_RE.match(slug):
        raise RemoteUrlError(
            f"Slug `{slug}` doesn't look right. Use letters, digits, "
            "dots, underscores, and hyphens."
        )

    api_base = f"{parsed.scheme}://{parsed.netloc}"
    return RemoteTarget(api_base=api_base, handle=handle, slug=slug, raw_url=url)


def parse_cloud_project(
    raw: str, *, default_handle: str | None = None
) -> tuple[str, str]:
    """Parse the ``cloud.project`` value from ``spec.yaml``.

    Two accepted forms:

    - ``<handle>/<slug>`` — preferred, fully qualified.
    - ``<slug>`` — legacy. Resolved against ``default_handle``
      (typically the signed-in user's handle from saved credentials).
      Raises ``RemoteUrlError`` if no handle can be derived, so users
      get a clear message instead of a confusing 404 from Cloud.

    Returns ``(handle, slug)``.
    """
    raw = (raw or "").strip()
    if not raw:
        raise RemoteUrlError(
            "`cloud.project` is empty. Set it to `<handle>/<slug>` "
            "(e.g. `acme/billing`)."
        )

    if "/" in raw:
        handle, _, slug = raw.partition("/")
        handle = handle.lower()
        if not _HANDLE_RE.match(handle):
            raise RemoteUrlError(
                f"`cloud.project: {raw}` — handle `{handle}` is malformed."
            )
        if not slug or not _SLUG_RE.match(slug):
            raise RemoteUrlError(
                f"`cloud.project: {raw}` — slug `{slug}` is malformed."
            )
        return handle, slug

    # Bare slug — fall back to the signed-in user's handle.
    if not default_handle:
        raise RemoteUrlError(
            f"`cloud.project: {raw}` is missing a handle and we don't "
            "know who you are yet. Either rewrite it as "
            f"`<handle>/{raw}` or run `spec login` so the CLI can "
            "fall back to your handle."
        )
    if not _HANDLE_RE.match(default_handle):
        raise RemoteUrlError(
            f"Saved handle `{default_handle}` is malformed. "
            "Re-run `spec login`."
        )
    if not _SLUG_RE.match(raw):
        raise RemoteUrlError(
            f"`cloud.project: {raw}` — slug looks malformed."
        )
    return default_handle, raw
