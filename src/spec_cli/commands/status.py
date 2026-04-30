"""`spec status` — list staged / modified / untracked / ignored files."""

from __future__ import annotations

import click

from ..config import BundleNotFoundError, find_bundle_root, load_manifest
from ..stage import classify_working_tree, load_index
from ..ui import console, dim, fatal


_STATE_ORDER = ["staged", "modified", "untracked", "deleted", "ignored", "clean"]
_STATE_STYLE = {
    "staged": "sf.mint",
    "modified": "yellow",
    "untracked": "sf.point",
    "deleted": "sf.reject",
    "ignored": "sf.muted",
    "clean": "sf.muted",
}
_STATE_LABEL = {
    "staged": "staged",
    "modified": "modified",
    "untracked": "untracked",
    "deleted": "deleted",
    "ignored": "ignored (not bundle content)",
    "clean": "clean",
}


@click.command("status")
@click.option("--all", "show_all", is_flag=True, help="Include clean files in the output.")
def status_cmd(show_all: bool) -> None:
    """Show what would be pushed from this bundle."""
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    manifest = load_manifest(root)
    idx = load_index(root)

    console.print(
        f"[sf.label]bundle[/] [bold]{manifest.name or root.name}[/] "
        f"[sf.muted]· {root}[/]"
    )

    lines = classify_working_tree(root, idx, manifest=manifest.data)
    if not lines:
        dim("Working tree is empty.")
        return

    grouped: dict[str, list] = {s: [] for s in _STATE_ORDER}
    for ln in lines:
        grouped[ln.state].append(ln)

    printed = False
    for state in _STATE_ORDER:
        if state == "clean" and not show_all:
            continue
        bucket = grouped[state]
        if not bucket:
            continue
        printed = True
        console.print()
        console.print(f"[{_STATE_STYLE[state]}]{_STATE_LABEL[state]}[/]")
        for ln in bucket:
            if ln.kind == "other":
                console.print(f"  {ln.rel}")
            else:
                console.print(f"  [sf.muted]{ln.kind:<9}[/]{ln.rel}")

    if not printed:
        dim("Nothing to do.")
