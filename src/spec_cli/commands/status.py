"""`spec status` — list staged / modified / untracked / ignored files."""

from __future__ import annotations

import click

from ..config import BundleNotFoundError, find_bundle_root, load_manifest
from ..constants import PROMPTS_DIRNAME
from ..git import read_git_context
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


def _print_pending_prompt_captures(root) -> None:
    """Surface "you have N new sessions to capture" before the working tree.

    Quiet when there's nothing — same tone as the rest of `spec status`.
    Imports the peek helper lazily so users without Claude Code or Cursor
    on disk pay nothing for this on every status invocation.
    """
    try:
        from .prompts import peek_pending_prompt_captures
    except Exception:  # noqa: BLE001
        return

    try:
        peek = peek_pending_prompt_captures(root)
    except Exception:  # noqa: BLE001
        return
    if peek is None:
        return

    console.print()
    console.print(
        f"[sf.warn]Prompts pending capture[/] "
        f"[sf.muted]· {peek.new_session_count} session(s), "
        f"{peek.new_turn_count} new turn(s) since last capture[/]"
    )
    for sid, title, model, asst_hint in peek.examples:
        short = sid[:12] + ("…" if len(sid) > 12 else "")
        model_bit = f"  [sf.muted]· session {model}[/]" if model else ""
        hint_bit = f"  [sf.muted]· {asst_hint}[/]" if asst_hint else ""
        console.print(f"  [sf.muted]·[/] {short}  {title}{model_bit}{hint_bit}")
    if peek.new_session_count > len(peek.examples):
        more = peek.new_session_count - len(peek.examples)
        dim(f"  · …and {more} more")
    if peek.assistant_models_line:
        dim(f"  Assistant models (pending turns): {peek.assistant_models_line}")
    dim(
        f"  → on next `spec add .` or `git commit`, these write to "
        f"{peek.dest_relpath}."
    )


def _print_unmerged_branch_prompts(root, *, current_branch: str) -> None:
    """When on trunk, list branch-prompts files that haven't been rolled up.

    Imports lazily and stays silent when (a) we're not on trunk or
    (b) there's nothing to roll. The matching nudge points the user at
    the post-merge hook (auto) or `spec prompts merge-branch` (manual).
    """
    try:
        from .prompts import (
            list_unmerged_branch_prompts,
            trunk_branch_for,
        )
    except Exception:  # noqa: BLE001
        return

    try:
        trunk = trunk_branch_for(root)
        if current_branch != trunk:
            return
        files = list_unmerged_branch_prompts(root, trunk=trunk)
    except Exception:  # noqa: BLE001
        return
    if not files:
        return

    console.print()
    console.print(
        f"[sf.warn]Branch prompts to roll into {PROMPTS_DIRNAME}/{trunk}.prompts[/] "
        f"[sf.muted]· {len(files)} file(s)[/]"
    )
    for p in files:
        try:
            console.print(f"  [sf.muted]·[/] {PROMPTS_DIRNAME}/{p.name}")
        except Exception:  # noqa: BLE001
            pass
    dim(
        "  → install the post-merge hook (run `spec git-hooks install`) "
        "or roll manually with `spec prompts merge-branch`."
    )


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

    git_ctx = read_git_context(root)
    branch_label = git_ctx.branch or ("(no git)" if not git_ctx.is_repo else "(detached)")
    console.print(
        f"[sf.label]bundle[/] [bold]{manifest.name or root.name}[/] "
        f"[sf.muted]· {root}[/] "
        f"[sf.muted]· branch [/][sf.point]{branch_label}[/]"
    )

    # Surface fresh agent-store activity (Cursor / Claude Code) before the
    # working-tree section. Mirrors the discovery half of `spec prompts
    # capture` without writing — the user gets a heads-up that their
    # `.prompts` is about to grow on the next `spec add` / `git commit`.
    _print_pending_prompt_captures(root)

    # When the user is on trunk, surface any non-trunk branch-prompts
    # files that should be rolled into trunk's `<trunk>.prompts`. Either
    # the post-merge hook didn't run (older bundle), or the branch was
    # squash-merged in a way that didn't trigger it.
    if git_ctx.is_repo and git_ctx.branch is not None:
        _print_unmerged_branch_prompts(root, current_branch=git_ctx.branch)

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
