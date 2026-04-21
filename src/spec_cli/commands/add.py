"""`spec add <paths…>` — stage files for the next push."""

from __future__ import annotations

from pathlib import Path

import click

from ..config import BundleNotFoundError, find_bundle_root
from ..constants import SPEC_EXTENSIONS, is_spec_file
from ..stage import load_index, rel_posix, save_index, sha256, walk_spec_files
from ..ui import dim, fatal, ok, reject


@click.command("add")
@click.argument("paths", nargs=-1, required=True, type=click.Path(exists=False))
def add_cmd(paths: tuple[str, ...]) -> None:
    """
    Stage files for the next push.

    Accepts file paths, directory paths, or `.`. Non-spec extensions are
    rejected explicitly — the CLI never silently skips a file the user asked
    for.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    idx = load_index(root)

    # Expand each arg into a concrete set of files we should touch.
    targets: list[Path] = []
    rejected: list[tuple[str, str]] = []

    for raw in paths:
        p = Path(raw)
        if not p.exists():
            rejected.append((raw, "not found"))
            continue

        if p.is_dir():
            targets.extend(walk_spec_files(p))
            continue

        resolved = p.resolve()
        rel = rel_posix(root, resolved)
        if not is_spec_file(rel):
            exts = ", ".join(sorted(SPEC_EXTENSIONS))
            rejected.append((raw, f"not a spec file (allowed: {exts}, spec.yaml)"))
            continue
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
