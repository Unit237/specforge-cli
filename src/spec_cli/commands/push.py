"""`spec push` — upload the staged snapshot to Spec Cloud."""

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
from ..constants import MAX_BATCH_SIZE
from ..git import read_git_context
from ..stage import (
    InvalidBundleError,
    assert_push_invariants,
    load_index,
    save_index,
    sha256,
)
from ..ui import console, dim, fatal, ok, reject, warn


def _chunk(seq, n):
    buf: list = []
    for item in seq:
        buf.append(item)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


@click.command("push")
@click.argument("remote_url", required=False, metavar="[URL]")
@click.option(
    "--project",
    "-p",
    default=None,
    help="Override cloud.project in the manifest. Accepts `<handle>/<slug>` "
    "or a bare slug (uses your handle from saved credentials).",
)
@click.option("--dry-run", is_flag=True, help="Show what would be pushed, don't upload.")
def push_cmd(remote_url: str | None, project: str | None, dry_run: bool) -> None:
    """Upload the staged snapshot to Spec Cloud.

    With no argument, pushes to the host in ~/.spec/credentials using
    `cloud.project` from spec.yaml (or --project). Pass a URL to push to
    an explicit remote, git-style:

      spec push https://spec.lightreach.io/acme/billing.git
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    manifest = load_manifest(root)
    idx = load_index(root)

    creds_for_handle = load_credentials()
    default_handle = creds_for_handle.user_handle if creds_for_handle else None

    # Resolve the target: URL wins over --project wins over manifest.
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
                "No cloud project configured. Add `cloud.project: <handle>/<slug>` "
                "to spec.yaml, pass --project <handle>/<slug>, or push to an "
                "explicit URL: spec push https://spec.lightreach.io/<handle>/<slug>"
            )
            return
        try:
            handle, slug = parse_cloud_project(raw, default_handle=default_handle)
        except RemoteUrlError as e:
            fatal(str(e))
            return

    if not idx.staged:
        dim("Nothing staged. Run `spec add <paths>` first.")
        return

    try:
        assert_push_invariants(root, idx.staged)
    except InvalidBundleError as e:
        fatal(str(e))
        return

    # Git context travels with every push, attached to every file row the
    # server stores. This is what makes per-file history branch-aware: the
    # cloud UI can render real git branches instead of a linear `v1..vN`.
    # Read-only, best-effort — outside a git worktree these fields are None
    # and Cloud handles that just fine.
    git = read_git_context(root)

    # Build the payload: every staged path → current bytes from disk.
    payload: list[dict[str, str | None]] = []
    for rel in sorted(idx.staged):
        abs_path = root / rel
        if not abs_path.is_file():
            reject(f"{rel} — file disappeared between add and push")
            continue
        content = abs_path.read_text(encoding="utf-8")
        payload.append(
            {
                "path": rel,
                "content": content,
                "branch": git.branch,
                "commit_sha": git.commit_sha,
            }
        )

    header_target = url_target.raw_url if url_target else f"{handle}/{slug}"
    git_desc = (
        f"{git.branch}@{git.commit_sha[:7]}"
        if git.branch and git.commit_sha
        else "no-git"
    )
    console.print(
        f"[sf.label]push[/] [bold]{header_target}[/] "
        f"[sf.muted]· {len(payload)} files · {git_desc}[/]"
    )
    for item in payload:
        dim(f"  {item['path']}")

    if dry_run:
        dim("\n--dry-run: skipping upload.")
        return

    creds = load_credentials()
    if not creds or not creds.access_token:
        fatal("Not signed in. Run `spec login` first.")
        return

    # If the URL points at a different host than our saved creds, the token
    # may not be valid there. We still try — the server's 401 is the source
    # of truth — but we flag it so the user isn't guessing at auth failures.
    if url_target and url_target.api_base.rstrip("/") != creds.api_base.rstrip("/"):
        warn(
            f"Pushing to {url_target.api_base} but you're signed in to "
            f"{creds.api_base}. If the server rejects the token, run "
            f"`SPEC_API={url_target.api_base} spec login` first."
        )
        creds = replace(creds, api_base=url_target.api_base)

    try:
        client = CloudClient(creds)
    except ApiError as e:
        fatal(str(e))
        return

    try:
        project_info = client.resolve_project(handle, slug)
    except ApiError as e:
        fatal(f"Could not resolve project '{handle}/{slug}': {e}")
        return
    project_id = project_info["id"]

    total_accepted = 0
    total_rejected: list[tuple[str, str]] = []

    with console.status("[sf.muted]Uploading…[/]", spinner="dots"):
        for chunk in _chunk(payload, MAX_BATCH_SIZE):
            try:
                result = client.batch_upload(project_id, chunk)
            except ApiError as e:
                fatal(str(e))
                return
            for row in result.get("results", []):
                # Server contract (`BundleFileBatchResult` in
                # `backend/app/schemas.py`) is `{ok: bool, error: str|null}`.
                # We tolerate either spelling so an older server speaking
                # `{status: "accepted"|"rejected", reason: …}` keeps
                # working — there's no shared package between the two
                # repos to lock the contract down.
                ok_flag = row.get("ok")
                if ok_flag is None:
                    ok_flag = row.get("status") == "accepted"
                if ok_flag:
                    total_accepted += 1
                    rel = row.get("path") or (row.get("file") or {}).get("path")
                    if rel is None:
                        continue
                    idx.pushed[rel] = sha256(
                        (root / rel).read_bytes()
                    )
                    idx.staged.pop(rel, None)
                else:
                    reason = row.get("error") or row.get("reason") or "rejected"
                    total_rejected.append(
                        (row.get("path") or "?", reason)
                    )

    save_index(idx)

    for path, reason in total_rejected:
        reject(f"{path} — {reason}")

    if total_accepted:
        ok(f"Pushed {total_accepted} file(s) to [bold]{handle}/{slug}[/]")
    if total_rejected:
        raise SystemExit(1)
