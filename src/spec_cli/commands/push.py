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
    help="Override cloud.project in the manifest.",
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
        slug = url_target.slug
    else:
        slug = project or manifest.cloud_project
        if not slug:
            fatal(
                "No cloud project configured. Add `cloud.project:` to spec.yaml, "
                "pass --project <slug>, or push to an explicit URL: "
                "spec push https://spec.lightreach.io/<slug>"
            )
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

    header_target = url_target.raw_url if url_target else slug
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
        project_info = client.resolve_project(slug)
    except ApiError as e:
        fatal(f"Could not resolve project '{slug}': {e}")
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
                if row.get("status") == "accepted":
                    total_accepted += 1
                    rel = row["path"]
                    # Advance 'pushed' to match what's now on the server.
                    idx.pushed[rel] = sha256(
                        (root / rel).read_bytes()
                    )
                    idx.staged.pop(rel, None)
                else:
                    total_rejected.append((row.get("path", "?"), row.get("reason", "rejected")))

    save_index(idx)

    for path, reason in total_rejected:
        reject(f"{path} — {reason}")

    if total_accepted:
        ok(f"Pushed {total_accepted} file(s) to [bold]{slug}[/]")
    if total_rejected:
        raise SystemExit(1)
