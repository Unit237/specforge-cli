"""`spec unstage` — drop paths from the staged set (the next `spec push`)."""

from __future__ import annotations

from pathlib import Path

import click

from ..config import BundleNotFoundError, find_bundle_root
from ..stage import load_index, rel_posix, save_index
from ..ui import dim, fatal, ok, reject


def _rels_to_unstage(root: Path, raw: str, staged: dict[str, str]) -> set[str]:
    """Map one user argument to staged keys to remove (may be many for a directory)."""
    out: set[str] = set()
    keys = frozenset(staged.keys())
    norm = raw.replace("\\", "/").strip()
    if norm in keys:
        return {norm}

    p = Path(raw)
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    if not p.exists():
        return out
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return out
    if p.is_file():
        rel = rel_posix(root, p)
        if rel in keys:
            return {rel}
        return out
    if p.is_dir():
        base = rel_posix(root, p)
        for k in keys:
            if k == base or k.startswith(f"{base}/"):
                out.add(k)
    return out


@click.command("unstage")
@click.argument(
    "paths",
    nargs=-1,
    required=True,
    type=click.Path(exists=False, file_okay=True, dir_okay=True),
)
def unstage_cmd(paths: tuple[str, ...]) -> None:
    """
    Remove paths from the staged snapshot (stored in ``.spec/index.json``).

    The stage list is **cumulative**: ``spec add`` appends. If a bad path
    (for example a ``.md`` under ``prompts/``) was added earlier, push fails
    until you ``unstage`` that path or fix the file layout — even if you later
    ``add`` a different file.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    idx = load_index(root)
    to_remove: set[str] = set()
    not_staged: list[str] = []

    for raw in paths:
        found = _rels_to_unstage(root, raw, idx.staged)
        if found:
            to_remove |= found
        else:
            not_staged.append(raw)

    for raw in not_staged:
        reject(f"{raw} — not in staged set")

    if not to_remove:
        if not_staged:
            raise SystemExit(1)
        dim("Nothing to unstage.")
        return

    for rel in sorted(to_remove):
        idx.staged.pop(rel, None)

    save_index(idx)
    for rel in sorted(to_remove):
        ok(f"unstaged [bold]{rel}[/]")

    if not_staged:
        raise SystemExit(1)
