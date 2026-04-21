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
    out = result.stdout.strip()
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


__all__ = ["GitContext", "read_git_context"]
