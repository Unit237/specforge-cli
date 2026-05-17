"""``spec bundle`` — manifest alignment with git / GitHub."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from ..api import ApiError, CloudClient
from ..config import (
    BundleNotFoundError,
    dump_manifest,
    find_bundle_root,
    load_credentials,
    load_manifest,
    parse_cloud_project,
    RemoteUrlError,
)
from ..git import read_origin_url, repo_name_from_remote_url, repo_toplevel
from ..ui import console, dim, fatal, info, ok, warn


@dataclass(frozen=True)
class _DoctorState:
    root: Path
    dir_name: str
    yaml_name: str | None
    origin_url: str | None
    origin_repo: str | None
    cloud_raw: str | None
    bundle_id: str | None
    cloud_project_bare_slug: bool
    cloud_handle: str | None
    cloud_slug: str | None
    cloud_parse_error: str | None


def _build_doctor_state(root: Path) -> _DoctorState:
    manifest = load_manifest(root)
    creds = load_credentials()
    dh = creds.user_handle if creds else None
    cloud_raw = manifest.cloud_project
    bundle_id = manifest.cloud_bundle_id
    origin_url = read_origin_url(root)
    bare = bool(
        cloud_raw and isinstance(cloud_raw, str) and "/" not in cloud_raw.strip()
    )
    ch: str | None = None
    cs: str | None = None
    parse_err: str | None = None
    if cloud_raw:
        try:
            ch, cs = parse_cloud_project(cloud_raw, default_handle=dh)
        except RemoteUrlError as e:
            parse_err = str(e)
    return _DoctorState(
        root=root,
        dir_name=root.name,
        yaml_name=manifest.name,
        origin_url=origin_url,
        origin_repo=repo_name_from_remote_url(origin_url),
        cloud_raw=cloud_raw if isinstance(cloud_raw, str) else None,
        bundle_id=bundle_id,
        cloud_project_bare_slug=bare,
        cloud_handle=ch,
        cloud_slug=cs,
        cloud_parse_error=parse_err,
    )


def _cloud_project_parse_hints(s: _DoctorState) -> list[str]:
    if not s.cloud_parse_error:
        return []
    return [f"cloud.project invalid: {s.cloud_parse_error}"]


def _local_alignment_hints(s: _DoctorState) -> list[str]:
    """Issues detectable without Cloud (used after ``git pull`` / ``--local-only``).

    Excludes invalid ``cloud.project`` parse errors — the CLI prints those
    once next to the cloud line; :func:`emit_bundle_doctor_post_merge_hints`
    prepends :func:`_cloud_project_parse_hints` instead.
    """
    hints: list[str] = []
    if s.origin_repo and s.yaml_name and s.origin_repo != s.yaml_name:
        hints.append(
            f"manifest `name` ({s.yaml_name!r}) differs from origin repo ({s.origin_repo!r}) "
            "— run `spec bundle sync-name` to align, or set `name` by hand."
        )
    if s.origin_repo and s.dir_name != s.origin_repo and (
        not s.yaml_name or s.yaml_name == s.dir_name
    ):
        hints.append(
            f"folder name ({s.dir_name!r}) differs from origin repo ({s.origin_repo!r}); "
            "the manifest `name` field is what Spec shows most often."
        )
    gt = repo_toplevel(s.root)
    if gt is not None and s.root.resolve() != gt.resolve():
        hints.append(
            f"bundle root ({s.root}) is not the git toplevel ({gt}) — unusual for a 1:1 repo."
        )
    return hints


def _probe_cloud_once(s: _DoctorState) -> tuple[list[str], dict[str, Any] | None]:
    """Single resolve; returns ``(hints, project)`` where ``project`` is set on success."""
    hints: list[str] = []
    creds = load_credentials()
    if not (s.cloud_handle and s.cloud_slug and creds and creds.access_token):
        return hints, None
    try:
        client = CloudClient(creds)
        proj = client.resolve_project(s.cloud_handle, s.cloud_slug)
        remote_bid = proj.get("bundle_id")
        if (
            s.bundle_id
            and remote_bid
            and str(s.bundle_id).strip() != str(remote_bid).strip()
        ):
            hints.append(
                f"Local `cloud.bundle_id` ({s.bundle_id!r}) differs from Cloud ({remote_bid!r}). "
                "Resolve before `spec push`: bind to the tree that matches Cloud, or adopt "
                "after a deliberate first push (see push command bundle mismatch error)."
            )
        return hints, proj
    except ApiError as e:
        st = getattr(e, "status", None)
        if s.cloud_project_bare_slug:
            hints.append(
                f"Cloud rejected `{s.cloud_handle}/{s.cloud_slug}` ({e}; status={st}). "
                f"`cloud.project` is the bare slug {s.cloud_raw!r}, so the CLI queried "
                f"`{s.cloud_handle}/{s.cloud_slug}` using **your** login — not the bundle owner's "
                "namespace. Fix: set `cloud.project` to `<owner-handle>/<slug>` (e.g. the handle "
                "shown on the team Bundles page) and commit."
            )
        else:
            hints.append(
                f"Your Spec login cannot use `{s.cloud_handle}/{s.cloud_slug}` ({e}; status={st}). "
                f"Ask `@{s.cloud_handle}` (or a workspace admin) to grant access, then re-run this command."
            )
        return hints, None


def emit_bundle_doctor_post_merge_hints(bundle_root: Path) -> None:
    """After ``git pull`` merge: print **local** alignment issues to stderr only.

    Skips Cloud so pulls stay fast and work offline. Idempotent and
    never raises — intended from :func:`run_git_hook_post_merge`.
    """
    try:
        state = _build_doctor_state(bundle_root)
    except Exception:
        return
    for h in _cloud_project_parse_hints(state) + _local_alignment_hints(state):
        sys.stderr.write(f"spec: bundle doctor (after merge) — {h}\n")
    try:
        sys.stderr.flush()
    except OSError:
        pass


@click.group("bundle")
def bundle_group() -> None:
    """Inspect and align bundle metadata with your git remote."""


@bundle_group.command("root")
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    help="Print only the resolved path; exit 1 when no bundle is found.",
)
def bundle_root_cmd(quiet: bool) -> None:
    """Print the Spec bundle root for the current working directory.

    Uses the same resolution as ``spec watch``: walk up for ``spec.yaml``,
    honor ``SPEC_BUNDLE_ROOT``, discover git-tracked bundles in a monorepo,
    then scan descendants when cwd is a parent of a nested bundle. Shell
    autostart calls this when the walk-up finds nothing.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        if quiet:
            raise SystemExit(1) from e
        fatal(str(e))
        return
    click.echo(str(root.resolve()))


@bundle_group.command("doctor")
@click.option(
    "--local-only",
    is_flag=True,
    help="Skip Cloud checks (no network). Git hook post-merge uses the same local rules.",
)
def bundle_doctor_cmd(local_only: bool) -> None:
    """Show ``spec.yaml`` identity, git alignment, and cloud linkage.

    Resolves the bundle from ``cwd`` the same way as ``spec watch`` (walk-up,
    ``SPEC_BUNDLE_ROOT``, git-tracked ``spec.yaml`` in a monorepo, or a
    descendant bundle when cwd is a wrapper folder). When
    logged in, asks Cloud whether your account can access ``cloud.project`` —
    the usual blocker for team clones before ``spec team watch`` / ``spec watch``.

    Use ``--local-only`` for a fast offline check; ``git pull`` runs the same
    local checks via the post-merge hook (hints only when something is off).
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    state = _build_doctor_state(root)
    parse_hints = _cloud_project_parse_hints(state)
    local_hints = _local_alignment_hints(state)
    creds = load_credentials()
    cloud_hints: list[str] = []
    proj: dict[str, Any] | None = None

    console.print("[sf.label]Bundle doctor[/]")
    console.print(f"  [sf.muted]root[/]       {state.root}")
    console.print(f"  [sf.muted]folder[/]    {state.dir_name}")
    console.print(f"  [sf.muted]name[/]      {state.yaml_name or '(unset)'}")
    if state.origin_url:
        console.print(f"  [sf.muted]origin[/]    {state.origin_url}")
        console.print(
            f"  [sf.muted]repo[/]      {state.origin_repo or '(unparsed)'}"
        )
    else:
        dim("  origin: (no remote.origin.url — cannot infer repo name)")

    if state.bundle_id:
        console.print(f"  [sf.muted]bundle_id[/] {state.bundle_id}")

    if state.cloud_raw:
        if state.cloud_parse_error:
            dim(f"  cloud: (invalid project line — {state.cloud_parse_error})")
        else:
            assert state.cloud_handle and state.cloud_slug
            console.print(
                f"  [sf.muted]cloud[/]     {state.cloud_handle}/{state.cloud_slug}"
            )
            if state.cloud_project_bare_slug:
                dim(
                    "  note: bare `cloud.project` uses **your** Spec handle + that slug on the API. "
                    "For bundles owned by someone else, commit `cloud.project: <their-handle>/<slug>`."
                )
    else:
        dim("  cloud: (cloud.project unset)")

    if not local_only and state.cloud_handle and state.cloud_slug and creds and creds.access_token:
        cloud_hints, proj = _probe_cloud_once(state)
        if proj is not None:
            pid = proj.get("id")
            remote_bid = proj.get("bundle_id")
            tail = f", remote bundle_id={remote_bid}" if remote_bid else ""
            console.print(
                f"  [sf.muted]cloud access[/]  ok — project id {pid}{tail}"
            )
    elif not local_only and state.cloud_handle and state.cloud_slug:
        dim("  cloud access: (run `spec login` to verify access)")

    all_hints = parse_hints + local_hints + cloud_hints
    if all_hints:
        console.print()
        for h in all_hints:
            warn(h)
    else:
        console.print()
        if (
            not local_only
            and state.cloud_handle
            and state.cloud_slug
            and creds
            and creds.access_token
        ):
            ok("no issues detected.")
        elif not local_only and state.cloud_handle and state.cloud_slug:
            ok("no issues detected (run `spec login` to verify cloud access).")
        else:
            ok("no issues detected.")


@bundle_group.command("sync-name")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the change without writing spec.yaml.",
)
def bundle_sync_name_cmd(dry_run: bool) -> None:
    """Set ``name`` in ``spec.yaml`` from ``git remote get-url origin``.

    Falls back with a clear error when origin is missing or unparsable.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    origin_url = read_origin_url(root)
    inferred = repo_name_from_remote_url(origin_url)
    if not inferred:
        fatal(
            "Cannot infer a repo name from origin. "
            "Configure `git remote add origin …` or set `name` in spec.yaml manually."
        )
        return

    manifest = load_manifest(root)
    current = manifest.name
    if current == inferred:
        ok(f"`name` already matches origin ({inferred!r}). Nothing to do.")
        return

    if dry_run:
        info(f"Would set `name` to {inferred!r} (currently {current!r}).")
        return

    manifest.data["name"] = inferred
    dump_manifest(manifest)
    ok(f"Updated spec.yaml `name` → {inferred!r}")
