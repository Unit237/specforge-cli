"""
Tiny read-only git introspection helpers.

Spec leans on the user's existing git workflow — we never run
`git commit`, `git checkout`, or any state-mutating command. All we need is
to *read* what git already knows: the current branch, the current commit
SHA, and the user's committer identity. Those are written into
`.prompts` files and sent up with every `spec push`.

Every function here fails quietly. If the bundle root isn't a git repo, if
git isn't installed, if the worktree is detached-HEAD — we return `None`
for that particular piece of data and the caller keeps going. We never
raise on the happy path, so a missing git never breaks `spec push` or
`spec prompts capture`.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GitContext:
    """What we know about the git state of a bundle root.

    All fields are optional. A `None` means "we couldn't determine this"
    — the caller writes whatever it can and moves on.
    """

    branch: str | None = None
    commit_sha: str | None = None
    author_name: str | None = None
    author_email: str | None = None
    is_repo: bool = False


def _run_git(args: list[str], *, cwd: Path) -> str | None:
    """Run a read-only git subcommand and return its stripped stdout, or
    `None` on any failure (non-zero exit, missing binary, etc.)."""
    if shutil.which("git") is None:
        return None
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip()
    return out or None


def read_git_context(root: Path) -> GitContext:
    """Gather the git context for a bundle root. Safe to call anywhere."""
    ctx = GitContext()

    # Cheapest check first: are we even inside a git worktree?
    is_repo = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=root)
    if is_repo != "true":
        return ctx
    ctx.is_repo = True

    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    # Detached HEAD surfaces as "HEAD" from `--abbrev-ref` — surface that
    # as `None` so Cloud doesn't record a meaningless branch name.
    if branch and branch != "HEAD":
        ctx.branch = branch

    sha = _run_git(["rev-parse", "HEAD"], cwd=root)
    if sha:
        ctx.commit_sha = sha

    ctx.author_name = _run_git(["config", "user.name"], cwd=root)
    ctx.author_email = _run_git(["config", "user.email"], cwd=root)

    return ctx


def read_origin_url(root: Path) -> str | None:
    """Return the URL configured for the ``origin`` remote, or ``None``.

    ``None`` covers every "we can't tell" case in one bucket — no git,
    no worktree, no ``origin``, transient subprocess failure — so the
    caller doesn't have to untangle them. Spec only consults this for
    name inference, where any failure should silently fall back to the
    directory name.
    """
    out = _run_git(["config", "--get", "remote.origin.url"], cwd=root)
    return out or None


def find_git_dir(start: Path) -> Path | None:
    """Resolve the ``.git`` directory for ``start``.

    Handles plain checkouts (``.git`` is a directory), worktrees, and
    submodules where ``.git`` is a ``gitdir:`` pointer file. Walks upward
    from ``start`` when ``.git`` is missing — best-effort when ``start``
    is a nested directory inside the worktree.
    """
    candidate = start / ".git"
    if candidate.is_dir():
        return candidate
    if candidate.is_file():
        try:
            first = candidate.read_text(encoding="utf-8").strip().splitlines()[0]
        except (OSError, UnicodeDecodeError):
            return None
        if first.startswith("gitdir:"):
            target = Path(first.split(":", 1)[1].strip())
            if not target.is_absolute():
                target = (start / target).resolve()
            if target.is_dir():
                return target
    parent = start.parent
    if parent != start:
        return find_git_dir(parent)
    return None


def repo_toplevel(root: Path) -> Path | None:
    """Resolve the worktree root via ``git rev-parse --show-toplevel``.

    Returns ``None`` when ``root`` isn't inside a git worktree. We
    prefer this over walking the filesystem for a ``.git`` because it
    handles git-worktree, submodule, and ``GIT_DIR`` setups uniformly —
    git already knows where the worktree starts and we don't.
    """
    out = _run_git(["rev-parse", "--show-toplevel"], cwd=root)
    if not out:
        return None
    p = Path(out)
    return p if p.is_dir() else None


__all__ = [
    "GitContext",
    "find_git_dir",
    "read_git_context",
    "read_origin_url",
    "repo_toplevel",
]
