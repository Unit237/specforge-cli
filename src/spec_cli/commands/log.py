"""`spec log` — print the push / run log for this bundle."""

from __future__ import annotations

from datetime import datetime

import click

from ..api import ApiError, CloudClient
from ..config import (
    BundleNotFoundError,
    find_bundle_root,
    load_credentials,
    load_manifest,
    parse_cloud_project,
    RemoteUrlError,
)
from ..ui import console, dim, fatal


def _fmt_when(raw: str | None) -> str:
    if not raw:
        return "—"
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M"
        )
    except ValueError:
        return raw


@click.command("log")
@click.option("--project", "-p", default=None, help="Override cloud.project.")
@click.option("-n", "limit", default=20, type=int, help="Number of entries to show.")
def log_cmd(project: str | None, limit: int) -> None:
    """Show recent pushes and runs for this bundle."""
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    manifest = load_manifest(root)
    raw_project = project or manifest.cloud_project
    if not raw_project:
        fatal("No cloud project configured.")
        return

    creds = load_credentials()
    if not creds or not creds.access_token:
        fatal("Not signed in. Run `spec login` first.")
        return

    default_handle = creds.user_handle
    try:
        handle, slug = parse_cloud_project(
            raw_project, default_handle=default_handle
        )
    except RemoteUrlError as e:
        fatal(str(e))
        return

    try:
        client = CloudClient(creds)
        info_ = client.resolve_project(handle, slug)
        entries = client.get_log(info_["id"])
    except ApiError as e:
        fatal(str(e))
        return

    if not entries:
        dim("No activity yet.")
        return

    for row in entries[:limit]:
        when = _fmt_when(row.get("created_at"))
        kind = row.get("kind", "event")
        who = row.get("actor") or "—"
        summary = row.get("summary") or ""
        console.print(
            f"[sf.muted]{when}[/]  "
            f"[sf.label]{kind:<7}[/]  "
            f"{summary}  "
            f"[sf.muted]· {who}[/]"
        )
