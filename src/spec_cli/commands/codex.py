"""`spec codex` — import recent Codex chats into `.prompts`."""

from __future__ import annotations

from datetime import datetime, timezone
import click

from ..config import BundleNotFoundError, find_bundle_root
from ..constants import PROMPTS_DIRNAME
from ..git import read_git_context
from ..prompts import PromptSchemaError, read_prompts_file
from ..sources import (
    CodexRecentSession,
    list_recent_codex_sessions,
    read_codex_rollout_session,
)
from ..stage import historical_bundle_paths, record_bundle_path
from ..ui import console, dim, fatal, info, pointer, warn
from .prompts import (
    _branch_prompts_path,
    _merge_into_branch_file,
    _stamp_capture_commit,
)


def _format_when(dt: datetime | None) -> str:
    if dt is None:
        return "unknown time"
    local = dt.astimezone()
    today = datetime.now(timezone.utc).astimezone().date()
    if local.date() == today:
        return local.strftime("today %H:%M")
    return local.strftime("%Y-%m-%d %H:%M")


def _print_recent(sessions: list[CodexRecentSession]) -> None:
    console.print("[sf.label]Recent Codex chats[/]")
    for i, s in enumerate(sessions, start=1):
        model = f" · {s.model}" if s.model else ""
        turns = f" · {s.turn_count} turn(s)" if s.turn_count else ""
        console.print(
            f"  [sf.point]{i}[/]. {_format_when(s.updated_at)} · "
            f"{s.title}{model}{turns}"
        )
        dim(f"      {s.cwd}")


def _choose_session(
    sessions: list[CodexRecentSession], *, index: int | None
) -> CodexRecentSession | None:
    if not sessions:
        return None
    if index is not None:
        if index < 1 or index > len(sessions):
            fatal(f"--index must be between 1 and {len(sessions)}")
            return None
        return sessions[index - 1]
    _print_recent(sessions)
    raw = click.prompt("Select chat number", default=1, type=int)
    if raw < 1 or raw > len(sessions):
        fatal(f"Select a number between 1 and {len(sessions)}")
        return None
    return sessions[raw - 1]


@click.group("codex")
def codex_group() -> None:
    """Capture Codex chats into this Spec bundle."""


@codex_group.command("capture")
@click.option(
    "--recent",
    type=int,
    default=10,
    show_default=True,
    metavar="N",
    help="Show the N most recent Codex chats for this bundle.",
)
@click.option(
    "--index",
    type=int,
    default=None,
    metavar="N",
    help="Capture the Nth recent chat without prompting.",
)
@click.option(
    "--summary-only",
    is_flag=True,
    help="Capture assistant summaries only, omitting response previews.",
)
@click.option("--dry-run", is_flag=True, help="Print what would be captured, don't write.")
def capture_cmd(
    recent: int,
    index: int | None,
    summary_only: bool,
    dry_run: bool,
) -> None:
    """Pick a recent Codex chat and append it as one `source = "codex"` session."""
    if recent < 1:
        fatal("--recent must be at least 1")
        return
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    record_bundle_path(root)
    paths_for_lookup = historical_bundle_paths(root)
    choices = list_recent_codex_sessions(paths_for_lookup, limit=recent)
    if not choices:
        dim("No recent Codex chats found for this bundle.")
        info("Open Codex in this project, chat once, then run `spec codex capture`.")
        return

    selected = _choose_session(choices, index=index)
    if selected is None:
        return

    session = read_codex_rollout_session(
        selected.path,
        verbose=not summary_only,
        title=selected.title,
        cwd=selected.cwd,
        model=selected.model,
    )
    if session is None:
        fatal(f"Could not read any user/assistant turns from {selected.path}")
        return

    git = read_git_context(root)
    author_name = git.author_name or "unknown"
    author_email = git.author_email or "unknown@unknown"
    branch = git.branch or "detached"
    if session.operator is None:
        session.operator = author_email
    _stamp_capture_commit(session, git=git, fallback_branch=branch)

    dest = _branch_prompts_path(root, branch)
    rel_dest = f"{PROMPTS_DIRNAME}/{dest.name}"
    existing_n = 0
    if dest.exists():
        try:
            existing_n = len(read_prompts_file(dest).sessions)
        except PromptSchemaError:
            existing_n = 0

    if dry_run:
        console.print(
            f"[sf.label]codex capture[/] [sf.muted]· "
            f"1 chat → {rel_dest} ({existing_n} already in file)[/]"
        )
        dim(f"  selected: {selected.title}")
        dim(f"  turns:    {len(session.turns)}")
        dim("  --dry-run: skipping write.")
        return

    try:
        changed, _ids = _merge_into_branch_file(
            dest,
            branch=branch,
            author_name=author_name,
            author_email=author_email,
            new_sessions=[session],
        )
    except PromptSchemaError as e:
        fatal(f"render failed: {e}")
        return

    if changed == 0:
        dim("No new Codex chat to capture (already present in branch file).")
        return

    console.print(
        f"[sf.label]codex capture[/] [sf.muted]· "
        f"{changed} chat → {rel_dest}[/]"
    )
    pointer("wrote", str(dest.relative_to(root)))
    warn(
        "Review the captured `.prompts` diff before committing if the chat "
        "contained sensitive context."
    )


__all__ = ["codex_group"]
