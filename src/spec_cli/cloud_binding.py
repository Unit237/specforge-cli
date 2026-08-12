"""Connect a local bundle to Spec Cloud without publishing its contents.

Watching and publishing are separate concerns. A watcher needs only an
authenticated, immutable Cloud bundle id; uploading staged files remains an
explicit ``spec publish`` operation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .api import ApiError, CloudClient
from .config import (
    Credentials,
    RemoteUrlError,
    dump_manifest,
    load_credentials,
    load_manifest,
    parse_cloud_project,
)


class CloudBindingError(RuntimeError):
    """A bundle could not be safely connected to Cloud."""


@dataclass(frozen=True)
class CloudBinding:
    root: Path
    project_id: int
    project: str
    bundle_id: str
    created: bool
    changed_manifest: bool


def _slugify(name: str) -> str:
    value = re.sub(r"[^\w\s-]", "", name.lower())
    return re.sub(r"[-\s]+", "-", value).strip("-") or "project"


def _not_found(error: ApiError) -> bool:
    if error.status == 404:
        return True
    if error.status != 400 or not isinstance(error.body, dict):
        return False
    detail = error.body.get("detail")
    return isinstance(detail, str) and "not found" in detail.casefold()


def ensure_cloud_binding(
    root: Path,
    *,
    credentials: Credentials | None = None,
) -> CloudBinding:
    """Resolve/create and stamp one bundle, without uploading any files.

    Auto-creation is deliberately limited to the signed-in user's own
    namespace. A qualified teammate/team target must already exist and be
    shared with the caller. Existing immutable ids are never replaced.
    """
    bundle_root = root.expanduser().resolve()
    manifest = load_manifest(bundle_root)
    creds = credentials or load_credentials()
    if not creds or not creds.access_token:
        raise CloudBindingError("Not signed in. Run `spec login` first.")

    raw_project = (manifest.cloud_project or "").strip()
    if not raw_project:
        name = str(manifest.name or bundle_root.name).strip() or bundle_root.name
        raw_project = _slugify(name)
    user_handle = getattr(creds, "user_handle", None)
    try:
        handle, slug = parse_cloud_project(
            raw_project,
            default_handle=user_handle,
        )
    except RemoteUrlError as exc:
        raise CloudBindingError(str(exc)) from exc

    client = CloudClient(creds)
    created = False
    try:
        project_info = client.resolve_project(handle, slug)
    except ApiError as exc:
        own_namespace = bool(
            user_handle
            and handle.casefold() == str(user_handle).strip().casefold()
        )
        if not (_not_found(exc) and own_namespace):
            raise CloudBindingError(
                f"Could not connect `{handle}/{slug}`: {exc}"
            ) from exc
        display_name = str(manifest.name or slug).strip() or slug
        create_name = display_name if _slugify(display_name) == slug else slug
        try:
            project_info = client.create_project(create_name)
        except ApiError as create_error:
            raise CloudBindingError(
                f"Could not create `{handle}/{slug}`: {create_error}"
            ) from create_error
        created = True

    project_id = int(project_info.get("id") or 0)
    remote_bundle_id = str(project_info.get("bundle_id") or "").strip()
    actual_slug = str(project_info.get("slug") or slug).strip()
    actual_handle = str(project_info.get("owner_handle") or handle).strip().lower()
    if project_id <= 0 or not remote_bundle_id or not actual_slug or not actual_handle:
        raise CloudBindingError("Cloud returned an incomplete bundle identity.")

    local_bundle_id = (manifest.cloud_bundle_id or "").strip()
    if local_bundle_id and local_bundle_id != remote_bundle_id:
        raise CloudBindingError(
            "Bundle mismatch: this working tree is already connected to "
            f"`{local_bundle_id}`, but `{actual_handle}/{actual_slug}` is "
            f"`{remote_bundle_id}`. Refusing to retarget it."
        )

    canonical_project = f"{actual_handle}/{actual_slug}"
    changed = False
    if (manifest.cloud_project or "").strip() != canonical_project:
        manifest.set_cloud_project(canonical_project)
        changed = True
    if local_bundle_id != remote_bundle_id:
        manifest.set_cloud_bundle_id(remote_bundle_id)
        changed = True
    if changed:
        try:
            dump_manifest(manifest)
        except OSError as exc:
            raise CloudBindingError(
                f"Cloud connected the bundle, but spec.yaml could not be updated: {exc}"
            ) from exc

    return CloudBinding(
        root=bundle_root,
        project_id=project_id,
        project=canonical_project,
        bundle_id=remote_bundle_id,
        created=created,
        changed_manifest=changed,
    )


__all__ = ["CloudBinding", "CloudBindingError", "ensure_cloud_binding"]
