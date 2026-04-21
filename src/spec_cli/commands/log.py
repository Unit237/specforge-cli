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
    slug = project or manifest.cloud_project
    if not slug:
        fatal("No cloud project configured.")
        return

    creds = load_credentials()
    if not creds or not creds.access_token:
        fatal("Not signed in. Run `spec login` first.")
        return

    try:
        client = CloudClient(creds)
        info_ = client.resolve_project(slug)
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
