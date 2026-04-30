"""`spec add <paths…>` — stage files for the next push."""

from __future__ import annotations

from pathlib import Path

import click

from ..config import BundleNotFoundError, find_bundle_root, load_manifest
from ..constants import (
    SPEC_EXTENSIONS,
    is_bundle_path,
    is_spec_file,
)
from ..frontmatter import read_frontmatter
from ..stage import load_index, rel_posix, save_index, sha256, walk_spec_files
from ..ui import dim, fatal, info, ok, reject


@click.command("add")
@click.argument("paths", nargs=-1, required=True, type=click.Path(exists=False))
@click.option(
    "--no-capture",
    is_flag=True,
    help=(
        "Skip the implicit `spec prompts capture` that runs before the walk "
        "when a directory is named. Use when you don't want fresh agent "
        "sessions swept into this commit."
    ),
)
def add_cmd(paths: tuple[str, ...], no_capture: bool) -> None:
    """
    Stage files for the next push.

    Accepts file paths, directory paths, or `.`. Bundle membership for
    `.md` files is decided by the resolver in
    :mod:`spec_cli.constants` — `AGENTS.md`, `CLAUDE.md`, anything under
    `docs/**` and anything pulled in by `spec.include` is bundle
    content; `README.md` and the rest of the well-known human-doc list
    is auxiliary by default. Auxiliary files are skipped during the
    directory walk; explicit `spec add path/to/README.md` still stages
    them (mirror of `git add -f`).

    When a directory argument is named (or `.`), `spec add` first runs
    the equivalent of `spec prompts capture` so any new Cursor /
    Claude Code sessions for this bundle are written to
    `prompts/captured/` *before* the walk. That's the natural
    expectation for "stage everything for this commit": the AI
    conversations that produced it are part of "everything". Pass
    `--no-capture` to opt out.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    manifest = load_manifest(root)
    idx = load_index(root)

    walking_a_dir = any(Path(p).is_dir() for p in paths)
    if walking_a_dir and not no_capture:
        # Auto-capture: turn new agent sessions into a fresh
        # `prompts/captured/<ts>.prompts` file before we walk the tree
        # so they get staged in the same `spec add` invocation.
        # Failures here never abort the add — capture is a convenience,
        # the user can always re-run `spec prompts capture` explicitly.
        from .prompts import run_auto_capture

        try:
            run_auto_capture(root)
        except Exception as e:  # noqa: BLE001
            dim(f"auto-capture skipped: {e}")

    targets: list[Path] = []
    rejected: list[tuple[str, str]] = []

    for raw in paths:
        p = Path(raw)
        if not p.exists():
            rejected.append((raw, "not found"))
            continue

        if p.is_dir():
            targets.extend(walk_spec_files(p, manifest=manifest.data))
            continue

        resolved = p.resolve()
        rel = rel_posix(root, resolved)
        if not is_spec_file(rel):
            exts = ", ".join(sorted(SPEC_EXTENSIONS))
            rejected.append((raw, f"not a spec file (allowed: {exts}, spec.yaml)"))
            continue

        # Explicit-path adds: an `.md` that fails the resolver is
        # warned-about but still staged (the user named it explicitly,
        # mirror of `git add -f`). The compiler will skip it on its own
        # pass via the same resolver, so the worst that happens is a
        # file in the bundle that doesn't show up in the compile prompt.
        suffix = resolved.suffix.lower()
        if suffix in (".md", ".markdown"):
            fm = read_frontmatter(resolved)
            if not is_bundle_path(rel, manifest=manifest.data, frontmatter=fm):
                info(
                    f"{raw} — auxiliary `.md` (excluded by default). "
                    "Staging anyway because you named it explicitly. To make "
                    "it bundle content, add `spec: true` frontmatter or list "
                    "it in spec.include."
                )
        targets.append(resolved)

    for raw, reason in rejected:
        reject(f"{raw} — {reason}")

    if not targets:
        if rejected:
            raise SystemExit(1)
        dim("Nothing matched.")
        return

    seen: set[str] = set()
    staged: list[str] = []
    unchanged: list[str] = []

    for t in targets:
        rel = rel_posix(root, t)
        if rel in seen:
            continue
        seen.add(rel)
        content = t.read_bytes()
        h = sha256(content)
        prev = idx.staged.get(rel)
        if prev == h:
            unchanged.append(rel)
            continue
        idx.staged[rel] = h
        staged.append(rel)

    save_index(idx)

    for rel in staged:
        ok(f"staged [bold]{rel}[/]")
    for rel in unchanged:
        dim(f"unchanged {rel}")

    if rejected and not staged:
        raise SystemExit(1)
