"""`spec status` — list staged / modified / untracked / ignored files."""

from __future__ import annotations

import click

from ..config import BundleNotFoundError, find_bundle_root, load_manifest
from ..stage import classify_working_tree, load_index, prune_stale_index_entries
from ..ui import console, dim, fatal


_STATE_ORDER = [
    "staged",
    "staged_stale",
    "unstaged_modified",
    "untracked",
    "deleted",
    "ignored",
    "clean",
]
_STATE_STYLE = {
    "staged": "sf.mint",
    "staged_stale": "yellow",
    "unstaged_modified": "yellow",
    "untracked": "sf.point",
    "deleted": "sf.reject",
    "ignored": "sf.muted",
    "clean": "sf.muted",
}
_STATE_LABEL = {
    "staged": "Staged for push",
    "staged_stale": "Modified (re-run spec add — snapshot out of date)",
    "unstaged_modified": "Not staged for push",
    "untracked": "Untracked",
    "deleted": "deleted",
    "ignored": "ignored (not bundle content)",
    "clean": "clean (matches last push)",
}


@click.command("status")
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Include clean and ignored files in the output.",
)
@click.option(
    "--ignored",
    "show_ignored",
    is_flag=True,
    help="Also list files that aren't bundle content (auxiliary docs, "
    "non-spec files). Hidden by default — they're noise during the "
    "common review-and-push loop.",
)
def status_cmd(show_all: bool, show_ignored: bool) -> None:
    """Show what would be pushed from this bundle.

    Section names mirror **git status**: **Staged for push** is the snapshot
    queued for ``spec push`` (like changes to be committed). **Modified**
    splits into paths whose staged hash is stale vs paths only tracked from
    a prior push that still need ``spec add`` — both mean "run ``spec add``"
    before push picks them up. By default, **clean** (matches last push) and
    **ignored** rows are hidden. Use ``--ignored`` / ``--all`` for full tree.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    manifest = load_manifest(root)
    idx = load_index(root)
    prune_stale_index_entries(idx, manifest=manifest.data)

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
        if state == "ignored" and not (show_all or show_ignored):
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
        return

    hidden_ignored = len(grouped["ignored"]) if not (show_all or show_ignored) else 0
    if hidden_ignored:
        console.print()
        dim(
            f"{hidden_ignored} ignored file"
            f"{'' if hidden_ignored == 1 else 's'} hidden — "
            "rerun with --ignored to inspect."
        )
