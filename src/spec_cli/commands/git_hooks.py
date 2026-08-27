"""Git hook helpers — `spec git-hooks pre-commit` / `commit-msg` / `pre-push`.

Installed by `spec init` so `git commit` mirrors spec staging, captures prompts
into the **same** commit (via the ``commit-msg`` hook), and `git push` runs
`spec push` for the same bundle.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

import click
import yaml

from ..config import (
    BundleNotFoundError,
    discover_bundle_roots_under_git_root,
    find_bundle_root,
    load_manifest,
)
from ..constants import MANIFEST_FILENAME, is_bundle_path, is_spec_file
from ..frontmatter import read_frontmatter
from ..git import repo_toplevel

from .prompts import run_capture_for_commit_msg_hook


def _spec_cmd_prefix() -> list[str]:
    exe = shutil.which("spec")
    if exe:
        return [exe]
    return [sys.executable, "-m", "spec_cli"]


def resolve_bundle_root_for_git_hook(git_top: Path | None = None) -> Path | None:
    """Pick the bundle root when git hooks run at the worktree top.

    Delegates to :func:`find_bundle_root` (``SPEC_BUNDLE_ROOT``, walk-up,
    then git-tracked discovery). Returns ``None`` when resolution fails so
    hooks can skip quietly; prints a hint when multiple bundles match.
    """
    top = (git_top or Path.cwd()).resolve()
    try:
        return find_bundle_root(top)
    except BundleNotFoundError:
        if not os.environ.get("SPEC_BUNDLE_ROOT", "").strip():
            candidates = discover_bundle_roots_under_git_root(top)
            if len(candidates) > 1:
                print(
                    "spec: multiple bundles in this repo; set SPEC_BUNDLE_ROOT to the bundle "
                    "directory, or run `spec push` manually.",
                    file=sys.stderr,
                )
        return None


def _repo_relative_path_under_bundle(
    repo_top: Path, bundle_root: Path, git_path_posix: str
) -> str | None:
    """Return ``git_path_posix`` relative to ``bundle_root``, or ``None`` if outside."""
    full = (repo_top / git_path_posix).resolve()
    bundle_res = bundle_root.resolve()
    try:
        return full.relative_to(bundle_res).as_posix()
    except ValueError:
        pass
    try:
        bundle_part = bundle_res.relative_to(repo_top).as_posix()
    except ValueError:
        return None
    gp = PurePosixPath(git_path_posix)
    bp = PurePosixPath(bundle_part)
    if gp == bp or str(gp).startswith(str(bp) + "/"):
        return gp.relative_to(bp).as_posix()
    return None


def _iter_git_diff_cached_name_status(repo_top: Path):
    """Yield ``(status, path1, path2)`` for staged changes.

    ``path2`` is set only for renames (``R*``) and copies (``C*``), per
    ``git diff --cached --name-status -z``. Other entries use
    ``path2 is None``.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_top),
                "diff",
                "--cached",
                "--name-status",
                "-z",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if result.returncode != 0:
        return
    raw = result.stdout or ""
    parts = raw.split("\0")
    i = 0
    n = len(parts)
    while i < n:
        if parts[i] == "":
            i += 1
            continue
        status = parts[i]
        i += 1
        if i >= n:
            break
        path1 = parts[i].strip().replace("\\", "/")
        i += 1
        if not path1:
            continue
        if len(status) >= 1 and status[0] in ("R", "C"):
            if i >= n:
                break
            path2 = parts[i].strip().replace("\\", "/")
            i += 1
            if path2:
                yield status, path1, path2
        else:
            yield status, path1, None


def _hook_should_drop_from_spec_index(inner: str, manifest: dict) -> bool:
    """Whether this bundle-relative path should be ``spec unstage``-d when git deletes/renames away."""
    if not is_spec_file(inner):
        return False
    suf = PurePosixPath(inner).suffix.lower()
    if suf in (".md", ".markdown"):
        return bool(is_bundle_path(inner, manifest=manifest, frontmatter=None))
    return True


def _hook_should_spec_add(bundle: Path, inner: str, manifest: dict) -> bool:
    """Mirror ``spec add`` eligibility: extension gate + bundle membership for ``.md``."""
    if not is_spec_file(inner):
        return False
    path = bundle / inner
    if not path.is_file():
        return False
    suf = PurePosixPath(inner).suffix.lower()
    if suf in (".md", ".markdown"):
        fm = read_frontmatter(path)
        return bool(is_bundle_path(inner, manifest=manifest, frontmatter=fm))
    return True


def _run_spec(bundle: Path, prefix: list[str], *subcmd: str, timeout: int = 120) -> None:
    subprocess.run(
        [*prefix, *subcmd],
        cwd=str(bundle),
        check=False,
        timeout=timeout,
    )


def _pre_push_includes_branch_ref(stdin_text: str) -> bool:
    """True if stdin lines include a ``refs/heads/`` push (not tags-only)."""
    if not stdin_text.strip():
        return True
    for line in stdin_text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        remote_ref = parts[2]
        if remote_ref.startswith("refs/heads/"):
            return True
    return False


def run_git_hook_pre_commit() -> None:
    """Capture new agent sessions, then mirror ``git add`` / ``git rm`` into spec staging.

    Capture runs **before** the index mirror loop on purpose. Two reasons:

    1. Capture writes ``prompts/<branch>.prompts`` and ``git add``s it.
       Doing this before the mirror loop means the mirror sees the
       freshly-staged path in ``git diff --cached`` and ``spec add``s
       it — single pass, no separate spec-staging code path.
    2. ``pre-commit`` runs before git locks the cache_tree it'll commit.
       Capturing here lets the new ``.prompts`` content land in the
       *same* commit, which is the whole point — capturing in
       ``commit-msg`` instead (the prior design) updated the index
       file but git ignored the change because the tree was already
       built. The user then had to commit twice. See
       :func:`run_capture_for_pre_commit_hook` for the design notes.

    Emits a single summary line (``spec: <changes>``) at the end so a
    user running ``git commit`` can actually *see* Spec doing its job;
    silent on a no-op commit (no captures, no bundle-eligible paths in
    the index). Visibility was the gap behind multiple "I committed but
    nothing seemed to happen on the spec side" reports — the work was
    happening, the user just couldn't see it.
    """
    top = repo_toplevel(Path.cwd())
    if top is None:
        return
    bundle = resolve_bundle_root_for_git_hook(top)
    if bundle is None:
        return

    from .prompts import run_capture_for_pre_commit_hook

    run_capture_for_pre_commit_hook(bundle, repo_top=top)

    try:
        manifest = load_manifest(bundle).data
    except (OSError, ValueError, yaml.YAMLError):
        manifest = {}
    prefix = _spec_cmd_prefix()

    # Summary counters — what the user gets to see at the end.
    n_added = 0
    n_unstaged = 0

    for status, path1, path2 in _iter_git_diff_cached_name_status(top):
        if not status:
            continue
        kind = status[0]

        # Renames and copies: two paths (old, new). Plain renames remove the old path.
        if kind in ("R", "C") and path2 is not None:
            old_inner = _repo_relative_path_under_bundle(top, bundle, path1)
            new_inner = _repo_relative_path_under_bundle(top, bundle, path2)
            if kind == "R" and old_inner and _hook_should_drop_from_spec_index(old_inner, manifest):
                _run_spec(bundle, prefix, "unstage", old_inner, timeout=60)
                n_unstaged += 1
            if new_inner and _hook_should_spec_add(bundle, new_inner, manifest):
                _run_spec(bundle, prefix, "add", new_inner)
                n_added += 1
            continue

        if kind == "D":
            inner = _repo_relative_path_under_bundle(top, bundle, path1)
            if inner is None:
                continue
            if _hook_should_drop_from_spec_index(inner, manifest):
                _run_spec(bundle, prefix, "unstage", inner, timeout=60)
                n_unstaged += 1
            continue

        # A/M/T/… — single-path updates (added, modified, type-change).
        inner = _repo_relative_path_under_bundle(top, bundle, path1)
        if inner is None:
            continue
        if _hook_should_spec_add(bundle, inner, manifest):
            _run_spec(bundle, prefix, "add", inner)
            n_added += 1

    if n_added or n_unstaged:
        # One line on stderr — visible alongside git's own output but
        # doesn't pollute things like `git commit -m "foo" 2>/dev/null`.
        # ``console.print`` would respect Rich theming, but pre-commit
        # may run under environments where Rich's autodetect mis-fires
        # (cron-like git GUIs); a plain print to stderr is the lowest-
        # common-denominator that the user always sees.
        bits: list[str] = []
        if n_added:
            bits.append(f"+{n_added} staged")
        if n_unstaged:
            bits.append(f"-{n_unstaged} unstaged")
        sys.stderr.write(f"spec: {' · '.join(bits)} (mirrored from git index)\n")
        sys.stderr.flush()


def run_git_hook_commit_msg(commit_msg_file: str) -> None:
    """Capture prompts into the pending git commit (never blocks the commit)."""
    path = Path(commit_msg_file)
    if not path.is_file():
        return
    try:
        message_bytes = path.read_bytes()
    except OSError:
        return
    top = repo_toplevel(Path.cwd())
    if top is None:
        return
    bundle = resolve_bundle_root_for_git_hook(top)
    if bundle is None:
        return
    run_capture_for_commit_msg_hook(
        bundle,
        repo_top=top,
        message_bytes=message_bytes,
    )


def run_git_hook_post_merge() -> None:
    """Roll non-trunk branch-prompts files into trunk's file when on trunk.

    Fires from the ``post-merge`` git hook after ``git merge`` (or the
    merge half of ``git pull``) succeeds. No-op when:

    * the bundle root can't be found,
    * the user isn't currently on the bundle's trunk branch, or
    * there are no non-trunk ``prompts/<slug>.prompts`` files to roll.

    Delegates to :func:`run_git_hook_post_merge_rollup` in
    :mod:`spec_cli.commands.prompts` for the actual file work.
    """
    top = repo_toplevel(Path.cwd())
    if top is None:
        return
    bundle = resolve_bundle_root_for_git_hook(top)
    if bundle is None:
        return
    from .bundle import emit_bundle_doctor_post_merge_hints
    from .prompts import run_git_hook_post_merge_rollup

    run_git_hook_post_merge_rollup(bundle)
    emit_bundle_doctor_post_merge_hints(bundle)


def run_git_hook_pre_push() -> int:
    """Run ``spec push`` for the resolved bundle. ``SKIP_SPEC_PUSH=1`` skips."""
    if os.environ.get("SKIP_SPEC_PUSH", "").strip() == "1":
        return 0
    top = repo_toplevel(Path.cwd())
    if top is None:
        return 0
    stdin_text = sys.stdin.read()
    if not _pre_push_includes_branch_ref(stdin_text):
        return 0
    bundle = resolve_bundle_root_for_git_hook(top)
    if bundle is None:
        return 0
    prefix = _spec_cmd_prefix()
    extra = os.environ.get("SPEC_HOOK_PUSH_EXTRA_ARGS", "").strip()
    extra_parts = shlex.split(extra) if extra else []
    args = [*prefix, "push", *extra_parts]
    result = subprocess.run(
        args,
        cwd=str(bundle),
        check=False,
        timeout=3600,
    )
    return int(result.returncode)


@click.group(
    "git-hooks",
    help=(
        "Install, remove, or run git hook entrypoints (normally installed by `spec init`)."
    ),
)
def git_hooks_group() -> None:
    pass


@git_hooks_group.command("pre-commit")
def git_hooks_pre_commit_cmd() -> None:
    """Sync spec staging with paths you staged in git (never blocks the commit)."""
    run_git_hook_pre_commit()


@git_hooks_group.command("commit-msg")
@click.argument("commit_msg_file", type=str)
def git_hooks_commit_msg_cmd(commit_msg_file: str) -> None:
    """Stage ``.prompts`` updates into the commit git is recording (hook entrypoint)."""
    run_git_hook_commit_msg(commit_msg_file)


@git_hooks_group.command("pre-push")
def git_hooks_pre_push_cmd() -> None:
    """Run ``spec push`` before git completes a branch push (respects ``SKIP_SPEC_PUSH``)."""
    raise SystemExit(run_git_hook_pre_push())


@git_hooks_group.command("post-merge")
def git_hooks_post_merge_cmd() -> None:
    """Roll non-trunk branch-prompts into trunk's file (run after ``git merge``)."""
    run_git_hook_post_merge()


@git_hooks_group.command("install")
def git_hooks_install_cmd() -> None:
    """Install or refresh Spec blocks in ``.git/hooks`` (same hooks as ``spec init``)."""
    from ..git import find_git_dir
    from ..ui import fatal, ok, pointer

    from .init import GIT_HOOK_INSTALL_ROWS, _install_git_hook_segment

    root = Path.cwd().resolve()
    try:
        find_bundle_root(root)
    except BundleNotFoundError:
        fatal(f"No {MANIFEST_FILENAME} near {root}. Run `spec init` first.")
        return

    git_dir = find_git_dir(root)
    if git_dir is None:
        fatal("Could not find .git — not a git repository?")
        return

    reports: list[tuple[str, str, Path]] = []
    try:
        for label, fname, beg, end, body, hdr in GIT_HOOK_INSTALL_ROWS:
            st, pth = _install_git_hook_segment(
                git_dir, fname, beg, end, body, fresh_header=hdr
            )
            reports.append((label, st, pth))
    except OSError as e:
        fatal(str(e))
        return

    ok("Spec git hooks refreshed.")
    for label, st, pth in reports:
        pointer(label, f"{pth} ({st})")


@git_hooks_group.command("uninstall")
def git_hooks_uninstall_cmd() -> None:
    """Remove Spec-managed blocks from ``.git/hooks`` (pre-commit, commit-msg, post-commit, pre-push)."""
    from ..git import find_git_dir
    from ..ui import dim, fatal, ok, pointer

    from .init import GIT_HOOK_INSTALL_ROWS, _uninstall_git_hook_segment

    root = Path.cwd().resolve()
    try:
        find_bundle_root(root)
    except BundleNotFoundError:
        fatal(f"No {MANIFEST_FILENAME} near {root}. Run `spec init` first.")
        return

    git_dir = find_git_dir(root)
    if git_dir is None:
        fatal("Could not find .git — not a git repository?")
        return

    reports: list[tuple[str, str, Path]] = []
    try:
        for label, fname, beg, end, _body, _hdr in GIT_HOOK_INSTALL_ROWS:
            st, pth = _uninstall_git_hook_segment(git_dir, fname, beg, end)
            reports.append((label, st, pth))
    except OSError as e:
        fatal(str(e))
        return

    ok("Spec git hooks removed (where present).")
    for label, st, pth in reports:
        pointer(label, f"{pth} ({st})")
    dim("Re-enable with: spec git-hooks install")


__all__ = [
    "discover_bundle_roots_under_git_root",
    "git_hooks_group",
    "resolve_bundle_root_for_git_hook",
    "run_git_hook_commit_msg",
    "run_git_hook_post_merge",
    "run_git_hook_pre_commit",
    "run_git_hook_pre_push",
]
