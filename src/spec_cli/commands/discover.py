"""`spec discover` — find Git repositories and initialize them in bulk."""

from __future__ import annotations

import os
from os.path import commonpath
from dataclasses import dataclass
from pathlib import Path

import click

from ..config import discover_bundle_roots_under_git_root
from ..constants import MANIFEST_FILENAME
from ..git import repo_toplevel
from ..preferences import load_preferences, remember_bundle, remember_discovery_root
from ..ui import dim, info, ok, warn
from .init import init_cmd
from .push import run_push_for_bundle


_DISCOVERY_SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".cache",
        ".mypy_cache",
        ".next",
        ".pytest_cache",
        ".ruff_cache",
        ".spec",
        ".tox",
        ".turbo",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "out",
        "target",
        "vendor",
        "venv",
    }
)


@dataclass(frozen=True)
class DiscoveredRepository:
    """A Git worktree and any Spec bundles already tracked inside it."""

    root: Path
    bundle_roots: tuple[Path, ...]

    @property
    def initialized(self) -> bool:
        return bool(self.bundle_roots)


def _has_own_git_marker(directory: Path) -> bool:
    marker = directory / ".git"
    return marker.is_dir() or marker.is_file()


def discover_git_repositories(search_root: Path, *, max_depth: int = 8) -> list[Path]:
    """Return Git worktree roots at or below ``search_root``.

    A ``.git`` file counts so linked worktrees and submodules are included.
    Expensive dependency/build/cache trees are pruned and symlinks are not
    followed. When invoked from a subdirectory of a repository, the containing
    worktree is included as well.
    """
    root = search_root.expanduser().resolve()
    found: set[Path] = set()

    containing = repo_toplevel(root)
    if containing is not None:
        found.add(containing.resolve())

    for current_raw, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(current_raw)
        try:
            depth = len(current.relative_to(root).parts)
        except ValueError:
            continue

        if ".git" in dirnames or ".git" in filenames or _has_own_git_marker(current):
            found.add(current.resolve())

        kept: list[str] = []
        if depth < max_depth:
            for name in dirnames:
                child = current / name
                if name in _DISCOVERY_SKIP_DIR_NAMES or name.startswith("."):
                    continue
                if child.is_symlink():
                    continue
                kept.append(name)
        dirnames[:] = kept

    return sorted(found, key=lambda path: (len(path.parts), str(path).lower()))


def inspect_git_repositories(roots: list[Path]) -> list[DiscoveredRepository]:
    """Classify repository roots without changing their worktrees."""
    repositories: list[DiscoveredRepository] = []
    for root in roots:
        bundles = set(discover_bundle_roots_under_git_root(root))
        # A fresh `spec init` manifest may not be git-tracked yet.
        if (root / MANIFEST_FILENAME).is_file():
            bundles.add(root)
        repositories.append(
            DiscoveredRepository(
                root=root,
                bundle_roots=tuple(sorted(bundles, key=lambda path: str(path).lower())),
            )
        )
    return repositories


def machine_discovery_roots(explicit_root: Path | None = None) -> list[Path]:
    """Return stable workspace roots for machine-wide discovery."""
    if explicit_root is not None:
        return [explicit_root.expanduser().resolve()]
    prefs = load_preferences()
    saved = [
        Path(value).expanduser().resolve()
        for value in prefs.discovery_roots
        if isinstance(value, str) and Path(value).expanduser().is_dir()
    ]
    if saved:
        return sorted(set(saved), key=lambda path: str(path).casefold())

    bundles = [
        Path(value).expanduser().resolve()
        for value in prefs.bundles
        if isinstance(value, str) and Path(value).expanduser().exists()
    ]
    if bundles:
        candidate = Path(commonpath([str(path) for path in bundles])).resolve()
        if candidate.is_file():
            candidate = candidate.parent
        home = Path.home().resolve()
        if candidate == Path(candidate.anchor) or home not in (candidate, *candidate.parents):
            candidate = home
        return [candidate]
    return [Path.home().resolve()]


def parse_repository_selection(raw: str, count: int) -> list[int]:
    """Parse ``all`` or a comma/space-separated set of numbers and ranges."""
    value = raw.strip().lower()
    if value in {"", "a", "all", "*"}:
        return list(range(count))
    if value in {"n", "none", "q", "quit"}:
        return []

    selected: set[int] = set()
    tokens = value.replace(",", " ").split()
    for token in tokens:
        if "-" in token:
            start_raw, end_raw = token.split("-", 1)
            if not start_raw.isdigit() or not end_raw.isdigit():
                raise ValueError(f"`{token}` is not a number or range")
            start, end = int(start_raw), int(end_raw)
            if start > end:
                raise ValueError(f"`{token}` must count upward")
            numbers = range(start, end + 1)
        else:
            if not token.isdigit():
                raise ValueError(f"`{token}` is not a number or range")
            numbers = (int(token),)
        for number in numbers:
            if number < 1 or number > count:
                raise ValueError(f"repository {number} is outside 1-{count}")
            selected.add(number - 1)
    return sorted(selected)


def _display_path(path: Path, search_root: Path) -> str:
    try:
        relative = path.relative_to(search_root)
    except ValueError:
        return str(path)
    return "." if not relative.parts else str(relative)


def _initialize_repository(ctx: click.Context, root: Path) -> bool:
    previous = Path.cwd()
    try:
        os.chdir(root)
        ctx.invoke(
            init_cmd,
            name=None,
            force=False,
            skip_git_hook=False,
            skip_gitignore=False,
            upgrade_rules=False,
        )
    except SystemExit as exc:
        if exc.code not in (None, 0):
            return False
    finally:
        os.chdir(previous)
    return True


@click.command("discover")
@click.argument(
    "root",
    required=False,
    type=click.Path(path_type=Path, exists=True, file_okay=False, resolve_path=True),
)
@click.option(
    "--all",
    "initialize_all",
    is_flag=True,
    help="Initialize every uninitialized repository without prompting.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="List repositories and their Spec status without changing files.",
)
@click.option(
    "--push",
    "push_after_init",
    is_flag=True,
    hidden=True,
    help="Push each successfully initialized repository to Spec Cloud.",
)
@click.option(
    "--max-depth",
    type=click.IntRange(1, 32),
    default=8,
    show_default=True,
    help="Maximum directory depth to scan below ROOT.",
)
@click.pass_context
def discover_cmd(
    ctx: click.Context,
    root: Path | None,
    initialize_all: bool,
    dry_run: bool,
    push_after_init: bool,
    max_depth: int,
) -> None:
    """Find Git repositories across this machine and initialize selected ones.

    With no ROOT, scans every saved workspace regardless of the current
    directory. The first run scans your home folder; passing ROOT remembers it
    for future rootless runs. Already-initialized repositories are registered
    but never overwritten. At the selection prompt, enter ALL, a list such as
    1,3,5, or ranges such as 1-4. Press Enter to select every uninitialized
    repository. Then run `spec on` once to connect and watch everything.
    """
    search_roots = machine_discovery_roots(root)
    if root is not None and not dry_run:
        remember_discovery_root(root)
    if len(search_roots) == 1:
        info(f"Scanning this machine under {search_roots[0]} …")
    else:
        info(f"Scanning {len(search_roots)} saved workspaces on this machine …")
        for search_root in search_roots:
            dim(f"  {search_root}")
    roots = sorted(
        {
            repository
            for search_root in search_roots
            for repository in discover_git_repositories(search_root, max_depth=max_depth)
        },
        key=lambda path: (len(path.parts), str(path).casefold()),
    )
    if not roots:
        ok("No Git repositories found.")
        return

    repositories = inspect_git_repositories(roots)
    pending = [repository for repository in repositories if not repository.initialized]
    if not dry_run:
        for repository in repositories:
            for bundle_root in repository.bundle_roots:
                remember_bundle(bundle_root)

    info("")
    info("Git repositories")
    selectable = 0
    for repository in repositories:
        label = (
            _display_path(repository.root, search_roots[0])
            if len(search_roots) == 1
            else str(repository.root)
        )
        if repository.initialized:
            count = len(repository.bundle_roots)
            detail = "Spec initialized" if count == 1 else f"Spec initialized ({count} bundles)"
            dim(f"  ✓     {label} · {detail}")
        else:
            selectable += 1
            info(f"  [{selectable}]   {label} · ready to initialize")

    info("")
    dim(
        f"Found {len(repositories)} Git repositor"
        f"{'y' if len(repositories) == 1 else 'ies'} · "
        f"{len(repositories) - len(pending)} initialized · {len(pending)} available"
    )
    if not pending:
        ok("Every discovered Git repository already has Spec and is registered.")
        return
    if dry_run:
        dim("Dry run — no repositories were changed.")
        return

    if initialize_all:
        indexes = list(range(len(pending)))
    else:
        while True:
            raw = click.prompt(
                "Select repositories (all, 1,3, or 1-4; none to cancel)",
                default="all",
                show_default=True,
            )
            try:
                indexes = parse_repository_selection(raw, len(pending))
                break
            except ValueError as exc:
                warn(str(exc))

    if not indexes:
        ok("No repositories selected; nothing changed.")
        return

    selected = [pending[index] for index in indexes]
    succeeded: list[Path] = []
    failed: list[Path] = []
    push_failed: list[Path] = []
    for position, repository in enumerate(selected, start=1):
        info("")
        info(f"[{position}/{len(selected)}] Initializing {repository.root}")
        if _initialize_repository(ctx, repository.root):
            succeeded.append(repository.root)
            if push_after_init:
                info(f"[{position}/{len(selected)}] Pushing {repository.root}")
                if run_push_for_bundle(
                    repository.root,
                    dry_run=False,
                    no_review=False,
                    reviewers=(),
                ):
                    push_failed.append(repository.root)
        else:
            failed.append(repository.root)

    info("")
    if succeeded:
        ok(
            f"Initialized {len(succeeded)} repositor"
            f"{'y' if len(succeeded) == 1 else 'ies'} and registered them with Spec."
        )
    for path in failed:
        warn(f"Could not initialize {path}")
    for path in push_failed:
        warn(f"Initialized but could not push {path}")
    if failed or push_failed:
        raise SystemExit(1)
    if push_after_init:
        ok(f"Pushed all {len(succeeded)} newly initialized repositories.")
    else:
        dim("Next: run `spec on` to connect and watch every registered project.")


__all__ = [
    "DiscoveredRepository",
    "discover_cmd",
    "discover_git_repositories",
    "inspect_git_repositories",
    "machine_discovery_roots",
    "parse_repository_selection",
]
