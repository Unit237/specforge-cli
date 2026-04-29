"""`spec pull` — mirror the latest server state into the working tree."""

from __future__ import annotations

from dataclasses import replace

import click

from ..api import ApiError, CloudClient
from ..config import (
    BundleNotFoundError,
    RemoteUrlError,
    find_bundle_root,
    load_credentials,
    load_manifest,
    parse_cloud_project,
    parse_remote_url,
)
from ..stage import load_index, save_index, sha256
from ..ui import console, dim, fatal, ok, reject, warn


@click.command("pull")
@click.argument("remote_url", required=False, metavar="[URL]")
@click.option("--project", "-p", default=None, help="Override cloud.project.")
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite locally-modified files. Without this flag, conflicts error.",
)
def pull_cmd(remote_url: str | None, project: str | None, force: bool) -> None:
    """
    Pull the latest bundle state from Cloud into the working tree.

    Symmetric with `push`: pass a URL to pull from an explicit remote
    (`spec pull https://spec.lightreach.io/acme/billing.git`). Without
    one, uses `cloud.project` from spec.yaml plus saved credentials.

    Three-way merges are out of scope for v0.1 (per PLAN §4). If any local
    file has diverged from the last known pushed hash AND the server has
    moved it forward, we error out with a list of conflicting paths.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    manifest = load_manifest(root)

    creds = load_credentials()
    if not creds or not creds.access_token:
        fatal("Not signed in. Run `spec login` first.")
        return

    url_target = None
    if remote_url:
        if project:
            fatal("Pass either a URL or --project, not both.")
            return
        try:
            url_target = parse_remote_url(remote_url)
        except RemoteUrlError as e:
            fatal(str(e))
            return
        handle, slug = url_target.handle, url_target.slug
    else:
        raw = project or manifest.cloud_project
        if not raw:
            fatal(
                "No cloud project configured. Pass --project <handle>/<slug>, "
                "or pull from an explicit URL: "
                "spec pull https://spec.lightreach.io/<handle>/<slug>"
            )
            return
        try:
            handle, slug = parse_cloud_project(
                raw, default_handle=creds.user_handle
            )
        except RemoteUrlError as e:
            fatal(str(e))
            return

    if url_target and url_target.api_base.rstrip("/") != creds.api_base.rstrip("/"):
        warn(
            f"Pulling from {url_target.api_base} but you're signed in to "
            f"{creds.api_base}. If the server rejects the token, run "
            f"`SPEC_API={url_target.api_base} spec login` first."
        )
        creds = replace(creds, api_base=url_target.api_base)

    try:
        client = CloudClient(creds)
        project_info = client.resolve_project(handle, slug)
        remote_files = client.list_files(
            project_info["id"], include_content=True
        )
    except ApiError as e:
        fatal(str(e))
        return

    idx = load_index(root)
    conflicts: list[str] = []
    changes: list[tuple[str, str]] = []  # (path, "new" | "updated" | "unchanged")

    for f in remote_files:
        rel = f["path"]
        remote_hash = f.get("content_hash")
        dest = root / rel
        local_hash: str | None = None
        if dest.is_file():
            local_hash = sha256(dest.read_bytes())

        if local_hash == remote_hash:
            changes.append((rel, "unchanged"))
            continue

        last_pushed = idx.pushed.get(rel)
        locally_modified = local_hash is not None and local_hash != last_pushed
        if locally_modified and not force:
            conflicts.append(rel)
            continue

        changes.append((rel, "new" if local_hash is None else "updated"))

    if conflicts:
        reject("Pull aborted — local changes would be overwritten:")
        for rel in conflicts:
            console.print(f"  {rel}")
        dim("Re-run with --force to overwrite, or commit/stash your changes first.")
        raise SystemExit(1)

    for f in remote_files:
        rel = f["path"]
        state = next((s for p, s in changes if p == rel), "unchanged")
        if state == "unchanged":
            continue
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f["content"], encoding="utf-8")
        idx.pushed[rel] = f.get("content_hash") or sha256(dest.read_bytes())
        idx.staged.pop(rel, None)
        ok(f"{state:<9} {rel}")

    save_index(idx)

    written = sum(1 for _, s in changes if s != "unchanged")
    if written == 0:
        dim("Already up to date.")
