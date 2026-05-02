"""
`spec prompts` — capture, submit, review, validate, and (soon) simulate
conversational sessions as `.prompts` files.

The lifecycle:

  1. `capture` writes auto-discovered sessions to `prompts/captured/`.
     Treat these as scrollback — low-signal advisory context for the
     compiler, not reviewed.
  2. `submit <file>` promotes a captured (or hand-written) prompt into
     `prompts/curated/_pending/`. The author commits + pushes; the pending
     file shows up in the PR's diff.
  3. A reviewer checks out the PR and runs `review`, which walks each
     pending file interactively and either accepts it (moves to
     `prompts/curated/`) or rejects it (deletes from the worktree). The
     reviewer commits + pushes the result; git history IS the audit log.
  4. `check --ci` gates merge: it exits non-zero whenever
     `prompts/curated/_pending/` is non-empty, so branch protection holds
     the PR until every prompt has been reviewed.

See `docs/prompt-format.md` for the file format contract and the review
lifecycle in prose.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import click

from ..config import BundleNotFoundError, find_bundle_root
from ..constants import (
    PROMPTS_CAPTURED_DIRNAME,
    PROMPTS_CURATED_DIRNAME,
    PROMPTS_DIRNAME,
    PROMPTS_PENDING_DIRNAME,
)
from ..git import (
    commit_gpgsign_enabled,
    predict_commit_object_sha,
    read_git_context,
)
from ..prompts import (
    CommitMeta,
    PromptSchemaError,
    PromptsFile,
    Session,
    SessionCommit,
    read_prompts_file,
)
from ..prompts.render import (
    branch_prompts_filename,
    prompts_filename,
    render_prompts_file,
)
from ..prompts.tiers import (
    Tier,
    captured_dir,
    classify_tier,
    count_tiers,
    curated_dir,
    iter_all_prompts,
    iter_pending,
    pending_dir,
    prompts_root,
)
from ..sources import (
    ClaudeCodeError,
    CursorError,
    claude_code_project_dir,
    cursor_workspace_storage_root,
    read_claude_code_sessions,
    read_cursor_sessions,
)
from ..sources.claude_code import claude_code_store_root
from ..stage import historical_bundle_paths, record_bundle_path, rel_posix
from ..ui import console, dim, fatal, info, ok, pointer, reject, warn


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_COMMIT_SHA_FROM_GIT = object()


def _git_stage_paths(repo_top: Path, paths: list[Path]) -> None:
    """``git add --`` each path relative to ``repo_top`` (no-op if git missing)."""
    if shutil.which("git") is None or not paths:
        return
    for p in paths:
        try:
            rel = p.resolve().relative_to(repo_top.resolve())
        except ValueError:
            continue
        subprocess.run(
            ["git", "-C", str(repo_top), "add", "--", str(rel)],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )


def _patch_branch_file_commit_shas(
    dest: Path,
    *,
    session_ids: frozenset[str],
    commit_sha: str | None,
) -> bool:
    """Rewrite ``commit_sha`` on selected sessions; return True if the file changed."""
    if not session_ids or not dest.exists():
        return False
    try:
        pf = read_prompts_file(dest)
    except PromptSchemaError:
        return False
    changed = False
    for s in pf.sessions:
        if s.id not in session_ids or s.commit is None:
            continue
        if s.commit.commit_sha != commit_sha:
            s.commit.commit_sha = commit_sha
            changed = True
    if not changed:
        return False
    dest.write_text(render_prompts_file(pf), encoding="utf-8")
    return True




def _prompts_dir(bundle_root: Path) -> Path:
    """Legacy helper — root prompts directory, used only by `validate`.

    New code should reach for the per-tier helpers in `prompts.tiers`.
    """
    return bundle_root / PROMPTS_DIRNAME


def _parse_since(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError as e:
        raise click.BadParameter(
            f"--since must be an ISO 8601 timestamp, got `{raw}`"
        ) from e


def _existing_session_ids(bundle_root: Path) -> set[str]:
    """Scan every `.prompts` file across every tier and return the set of
    session ids already captured.

    `prompts capture` is idempotent — once a session id shows up in any
    `.prompts` file (captured, curated, pending, or legacy-root), we never
    emit it again. This is what makes `capture` safe to run on a cron.
    """
    seen: set[str] = set()
    for tp in iter_all_prompts(bundle_root):
        try:
            pf = read_prompts_file(tp.abs_path)
        except PromptSchemaError:
            # Don't crash capture on a corrupt existing file — the user
            # gets that error from `prompts validate`.
            continue
        for s in pf.sessions:
            seen.add(s.id)
    return seen


def _branch_prompts_path(bundle_root: Path, branch: str) -> Path:
    """Resolve the canonical `prompts/<branch-slug>.prompts` path.

    v0.2 keeps every branch's captured sessions in one append-only
    file at the root of `prompts/`. The `prompts/captured/` and
    `prompts/curated/` tier directories are still honoured by the
    parser for back-compat with existing bundles, but new captures
    flow into the branch file directly.
    """
    return prompts_root(bundle_root) / branch_prompts_filename(branch)


def _stamp_capture_commit(
    session: Session,
    *,
    git,
    fallback_branch: str,
    commit_sha: str | None | object = _COMMIT_SHA_FROM_GIT,
) -> None:
    """Stamp per-session commit context onto a freshly-discovered Session.

    The agent-side adapters (`read_claude_code_sessions`,
    `read_cursor_sessions`) don't know about git — they just read the
    local store. We attach the *current* git context here so each
    session in the captured file carries its own attribution, even
    when the file ends up holding many commits over time.

    Pass ``commit_sha=None`` for hook flows that fill the real SHA after
    ``git add`` (see ``run_capture_for_commit_msg_hook``).
    """
    if session.commit is not None:
        return
    resolved: str | None
    if commit_sha is _COMMIT_SHA_FROM_GIT:
        resolved = git.commit_sha
    else:
        resolved = commit_sha  # type: ignore[assignment]
    session.commit = SessionCommit(
        branch=git.branch or fallback_branch,
        commit_sha=resolved,
        author_name=git.author_name,
        author_email=git.author_email,
    )


def _merge_into_branch_file(
    dest: Path,
    *,
    branch: str,
    author_name: str,
    author_email: str,
    new_sessions: list[Session],
) -> tuple[int, frozenset[str]]:
    """Append `new_sessions` to the branch file at `dest`.

    Idempotent on session id: a session already present in the file
    is dropped. Re-renders the whole file deterministically so two
    captures with the same input produce byte-identical output.

    Returns ``(added_count, ids_appended)``.
    """
    existing: PromptsFile | None
    if dest.exists():
        try:
            existing = read_prompts_file(dest)
        except PromptSchemaError:
            existing = None
    else:
        existing = None

    if existing is None:
        merged = PromptsFile(
            commit=CommitMeta(
                branch=branch,
                author_name=author_name,
                author_email=author_email,
            ),
            sessions=list(new_sessions),
            edits=[],
        )
        added = len(new_sessions)
    else:
        seen_ids = {s.id for s in existing.sessions}
        appended: list[Session] = []
        for s in new_sessions:
            if s.id in seen_ids:
                continue
            appended.append(s)
            seen_ids.add(s.id)
        merged = PromptsFile(
            commit=existing.commit,
            sessions=existing.sessions + appended,
            edits=existing.edits,
        )
        added = len(appended)
        appended_ids = frozenset(s.id for s in appended)

    if added == 0:
        return 0, frozenset()

    body = render_prompts_file(merged)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body, encoding="utf-8")
    if existing is None:
        appended_ids = frozenset(s.id for s in new_sessions)
    return added, appended_ids


def run_auto_capture(bundle_root: Path) -> Path | None:
    """Run capture as a side-effect of another command (e.g. `spec add .`).

    Returns the path of the branch's `.prompts` file when at least
    one new session was added, or `None` when there was nothing new
    (the common case on a quiet bundle). Either way, the function is
    silent unless something interesting happened — `spec add`'s output
    is the user's mental anchor; capture noise would drown it out.

    v0.2 writes to `prompts/<branch-slug>.prompts` (one file per
    branch, append-only). Trunk's file accumulates direct-to-trunk
    sessions; branch files accumulate work-in-progress, and Cloud's
    branch-merge endpoint promotes a branch's sessions into trunk's
    file with `merged_from` set so reviewed sessions show the green-
    dot signal in the UI.

    Failure modes are deliberately swallowed at the call site
    (`spec add` wraps this in a try/except and only prints the error).
    A missing agent store, a network error talking to git, an empty
    discovered list — none of these should abort `spec add`.
    """
    record_bundle_path(bundle_root)
    paths_for_lookup = historical_bundle_paths(bundle_root)

    claude_store = claude_code_store_root()
    cursor_store = cursor_workspace_storage_root()
    claude_available = claude_store.exists()
    cursor_available = cursor_store.exists()
    if not claude_available and not cursor_available:
        return None

    git = read_git_context(bundle_root)
    author_name = git.author_name or "unknown"
    author_email = git.author_email or "unknown@unknown"
    branch = git.branch or "detached"

    already = _existing_session_ids(bundle_root)
    discovered: list[Session] = []

    if claude_available:
        try:
            for session in read_claude_code_sessions(
                paths_for_lookup, since=None, verbose=True
            ):
                if session.id in already:
                    continue
                already.add(session.id)
                discovered.append(session)
        except ClaudeCodeError:
            return None

    if cursor_available:
        try:
            for session in read_cursor_sessions(
                paths_for_lookup, since=None, verbose=True
            ):
                if session.id in already:
                    continue
                already.add(session.id)
                discovered.append(session)
        except CursorError:
            return None

    if not discovered:
        return None

    for s in discovered:
        if s.operator is None:
            s.operator = author_email
        _stamp_capture_commit(s, git=git, fallback_branch=branch)

    dest = _branch_prompts_path(bundle_root, branch)
    try:
        added, _appended_ids = _merge_into_branch_file(
            dest,
            branch=branch,
            author_name=author_name,
            author_email=author_email,
            new_sessions=discovered,
        )
    except PromptSchemaError:
        return None

    if added == 0:
        return None

    rel_dest = f"{PROMPTS_DIRNAME}/{dest.name}"
    info(f"captured {added} new session(s) → {rel_dest}")
    return dest


def run_capture_for_commit_msg_hook(
    bundle_root: Path,
    *,
    repo_top: Path,
    message_bytes: bytes,
) -> None:
    """Run capture while git is building a commit (``commit-msg`` hook).

    Writes sessions with ``commit_sha`` unset, ``git add``s the branch
    ``.prompts`` file so it enters the index, predicts the pending commit
    SHA from the message bytes git passed to the hook, then patches the
    file and stages again. Swallows nearly all failures — hooks must not
    block ``git commit``.
    """
    try:
        skip_predict = commit_gpgsign_enabled(repo_top)
        record_bundle_path(bundle_root)
        paths_for_lookup = historical_bundle_paths(bundle_root)

        claude_store = claude_code_store_root()
        cursor_store = cursor_workspace_storage_root()
        claude_available = claude_store.exists()
        cursor_available = cursor_store.exists()
        if not claude_available and not cursor_available:
            dim("No coding-agent stores found on this machine.")
            return

        git = read_git_context(bundle_root)
        author_name = git.author_name or "unknown"
        author_email = git.author_email or "unknown@unknown"
        branch = git.branch or "detached"
        warn_non_git = not git.is_repo

        already_captured = _existing_session_ids(bundle_root)
        discovered: list[Session] = []

        if claude_available:
            try:
                for session in read_claude_code_sessions(
                    paths_for_lookup, since=None, verbose=True
                ):
                    if session.id in already_captured:
                        continue
                    already_captured.add(session.id)
                    discovered.append(session)
            except ClaudeCodeError:
                return

        if cursor_available:
            try:
                for session in read_cursor_sessions(
                    paths_for_lookup, since=None, verbose=True
                ):
                    if session.id in already_captured:
                        continue
                    already_captured.add(session.id)
                    discovered.append(session)
            except CursorError:
                return

        if not discovered:
            dim("No new sessions to capture.")
            return

        for s in discovered:
            if s.operator is None:
                s.operator = author_email
            _stamp_capture_commit(
                s, git=git, fallback_branch=branch, commit_sha=None
            )

        dest = _branch_prompts_path(bundle_root, branch)
        try:
            added, appended_ids = _merge_into_branch_file(
                dest,
                branch=branch,
                author_name=author_name,
                author_email=author_email,
                new_sessions=discovered,
            )
        except PromptSchemaError:
            return

        if added == 0:
            dim("No new sessions to capture (already present in branch file).")
            return

        rel_dest = f"{PROMPTS_DIRNAME}/{dest.name}"
        console.print(
            f"[sf.label]prompts capture[/] [sf.muted]· "
            f"{added} new session(s) → {rel_dest}[/]"
        )
        if warn_non_git:
            dim(
                "Not a git worktree — writing `branch=detached` and "
                "`author=unknown` into [commit]. You'll want to hand-edit "
                "those before pushing."
            )

        _git_stage_paths(repo_top, [dest])

        predicted: str | None = None
        if not skip_predict:
            predicted = predict_commit_object_sha(repo_top, message_bytes)

        if predicted and appended_ids:
            if _patch_branch_file_commit_shas(
                dest,
                session_ids=appended_ids,
                commit_sha=predicted,
            ):
                _git_stage_paths(repo_top, [dest])

        pointer("wrote", str(dest.relative_to(bundle_root)))
        dim(
            "Branch captures are append-only. Push from a non-trunk branch "
            "and open a review in Spec Cloud to merge into trunk's prompts."
        )
    except (OSError, subprocess.SubprocessError):
        return


# ---------------------------------------------------------------------------
# `spec prompts` command group
# ---------------------------------------------------------------------------


@click.group("prompts")
def prompts_group() -> None:
    """Capture and review conversational sessions.

    Prompt history is a build input, not telemetry. The compiler reads these
    files, and you can edit them to change what the next compile produces.
    """


# ---------------------------------------------------------------------------
# `prompts capture`
# ---------------------------------------------------------------------------


@prompts_group.command("capture")
@click.option(
    "--source",
    type=click.Choice(["claude_code", "cursor", "all"], case_sensitive=False),
    default="all",
    help="Restrict capture to one source (claude_code, cursor) or read both with `all`.",
)
@click.option(
    "--since",
    default=None,
    help="Only sessions started after this ISO 8601 timestamp (e.g. 2026-04-01T00:00:00Z).",
)
@click.option(
    "--max-sessions",
    type=int,
    default=None,
    metavar="N",
    help="When many sessions are new (typical on first run), only include the N most recent by start time.",
)
@click.option(
    "--summary-only",
    is_flag=True,
    help=(
        "Capture only a one-sentence `summary` of each assistant turn. "
        "Default is to also include a bounded `text` preview of the AI "
        "response so reviewers can see what the agent actually said."
    ),
)
@click.option(
    "--verbose",
    "verbose_capture",
    is_flag=True,
    help=(
        "Deprecated alias for the new default behaviour (capture preview "
        "text). Kept for back-compat with existing scripts."
    ),
)
@click.option("--dry-run", is_flag=True, help="Print counts, don't write any file.")
def capture_cmd(
    source: str,
    since: str | None,
    max_sessions: int | None,
    summary_only: bool,
    verbose_capture: bool,
    dry_run: bool,
) -> None:
    """Snapshot every new conversational session into one `.prompts` file.

    Only reads the Claude Code store folder for *this* bundle (see
    ``~/.claude/projects/<encoded-path>``); other repos are not included.
    The first time you run capture, every session in that folder that has
    not yet been written to a ``.prompts`` file is included, which can be
    many. Use ``--max-sessions`` or ``--since`` to cap the batch.

    Writes ``prompts/<branch-slug>.prompts`` with a ``[commit]`` block (from
    your git context) plus one ``[[sessions]]`` block per discovered session.
    With ``spec init`` hooks, this runs from git's ``commit-msg`` hook so new
    prompts bytes are part of the same commit; sessions already captured in
    any prior ``.prompts`` file are skipped.

    By default each assistant turn carries a bounded `text` preview
    (first ~3 KB of the response), in addition to the one-sentence
    `summary`. Pass `--summary-only` to fall back to summary-only
    capture; the `--verbose` flag is kept as a no-op alias for the
    new default. Sessions with text are marked `verbose = true` per
    the prompts schema.
    """
    # `--verbose` used to be required to capture assistant text; it's
    # now the implicit default. Either flag or its absence yields the
    # same result; only `--summary-only` opts out.
    _ = verbose_capture
    capture_text = not summary_only
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    if max_sessions is not None and max_sessions < 1:
        fatal("--max-sessions must be at least 1")
        return

    since_dt = _parse_since(since)

    # Remember where the bundle currently lives. On the first move /
    # rename after this point, the next capture will see *both* paths
    # in the historical list and find sessions written under either
    # location (Fix #2).
    record_bundle_path(root)
    paths_for_lookup = historical_bundle_paths(root)

    requested = source.lower()
    if requested not in ("claude_code", "cursor", "all"):
        fatal(f"Unknown source `{source}`.")
        return
    want_claude = requested in ("claude_code", "all")
    want_cursor = requested in ("cursor", "all")

    # Probe each requested store. Missing stores aren't errors when
    # `--source all` is the default — a user with only Cursor (or only
    # Claude Code) installed should still be able to capture.
    claude_store = claude_code_store_root()
    cursor_store = cursor_workspace_storage_root()
    claude_available = want_claude and claude_store.exists()
    cursor_available = want_cursor and cursor_store.exists()

    if requested == "claude_code" and not claude_available:
        dim(f"Claude Code store not found at {claude_store}.")
        info("Install Claude Code and start a session, then re-run `spec prompts capture`.")
        info("  https://claude.ai/code")
        return
    if requested == "cursor" and not cursor_available:
        dim(f"Cursor workspace store not found at {cursor_store}.")
        info("Open this bundle in Cursor and chat at least once, then re-run `spec prompts capture`.")
        return
    if not claude_available and not cursor_available:
        dim("No coding-agent stores found on this machine.")
        dim(f"  Claude Code: {claude_store}")
        dim(f"  Cursor:      {cursor_store}")
        info("Install Claude Code or Cursor, start a session, then re-run.")
        return

    # Capture context: git state + user identity. `read_git_context` fails
    # quietly on non-git bundles, so we fall back to whatever's available.
    git = read_git_context(root)
    if not git.is_repo:
        warn_non_git = True
    else:
        warn_non_git = False

    author_name = git.author_name or "unknown"
    author_email = git.author_email or "unknown@unknown"
    branch = git.branch or "detached"

    already_captured = _existing_session_ids(root)

    discovered = []
    if claude_available:
        try:
            for session in read_claude_code_sessions(
                paths_for_lookup, since=since_dt, verbose=capture_text
            ):
                if session.id in already_captured:
                    continue
                already_captured.add(session.id)
                discovered.append(session)
        except ClaudeCodeError as e:
            fatal(str(e))
            return

    if cursor_available:
        try:
            for session in read_cursor_sessions(
                paths_for_lookup, since=since_dt, verbose=capture_text
            ):
                if session.id in already_captured:
                    continue
                already_captured.add(session.id)
                discovered.append(session)
        except CursorError as e:
            fatal(str(e))
            return

    if not discovered:
        dim("No new sessions to capture.")
        return

    if max_sessions is not None and len(discovered) > max_sessions:
        tmin = datetime.min.replace(tzinfo=timezone.utc)

        def _start_key(s: Session) -> datetime:
            return s.started_at if s.started_at is not None else tmin

        by_new = sorted(discovered, key=_start_key, reverse=True)[: max_sessions]
        by_new.sort(
            key=lambda s: (
                s.started_at is None,
                s.started_at or datetime.max.replace(tzinfo=timezone.utc),
                s.id,
            ),
        )
        discovered = by_new
        warn(
            f"Only the {max_sessions} most recent session(s) are included (--max-sessions)."
        )
    elif len(discovered) > 20 and since is None:
        warn(
            f"Capturing {len(discovered)} new session(s) (everything not yet in a .prompts file for this folder). "
            "To limit: use --max-sessions=N or --since=2026-04-01T00:00:00Z."
        )
        if claude_available:
            dim(
                f"Claude session store for this bundle: {claude_code_project_dir(root)}"
            )
        if cursor_available:
            dim(f"Cursor workspace store: {cursor_store}")

    # Tag each session with who drove it. With git identity alone this is
    # a best guess; when credentials are linked to Cloud, the username is
    # written in `[commit].author_username` separately. Per-session
    # `[sessions.commit]` snapshots the current git state so the file
    # can hold sessions from many commits without losing attribution.
    for s in discovered:
        if s.operator is None:
            s.operator = author_email
        _stamp_capture_commit(s, git=git, fallback_branch=branch)

    dest = _branch_prompts_path(root, branch)
    rel_dest = f"{PROMPTS_DIRNAME}/{dest.name}"

    if dry_run:
        existing_n = 0
        if dest.exists():
            try:
                existing_n = len(read_prompts_file(dest).sessions)
            except PromptSchemaError:
                existing_n = 0
        console.print(
            f"[sf.label]prompts capture[/] [sf.muted]· "
            f"{len(discovered)} new session(s) → {rel_dest}"
            f" ({existing_n} already in file)[/]"
        )
        if warn_non_git:
            dim(
                "Not a git worktree — writing `branch=detached` and "
                "`author=unknown` into [commit]. You'll want to hand-edit "
                "those before pushing."
            )
        dim("\n--dry-run: skipping write.")
        return

    try:
        added, _appended_ids = _merge_into_branch_file(
            dest,
            branch=branch,
            author_name=author_name,
            author_email=author_email,
            new_sessions=discovered,
        )
    except PromptSchemaError as e:
        fatal(f"render failed: {e}")
        return

    if added == 0:
        # Every discovered session was already in the file (idempotent
        # re-run). Stay quiet — no diff for the user to look at.
        dim("No new sessions to capture (already present in branch file).")
        return

    console.print(
        f"[sf.label]prompts capture[/] [sf.muted]· "
        f"{added} new session(s) → {rel_dest}[/]"
    )
    if warn_non_git:
        dim(
            "Not a git worktree — writing `branch=detached` and "
            "`author=unknown` into [commit]. You'll want to hand-edit "
            "those before pushing."
        )

    pointer("wrote", str(dest.relative_to(root)))
    dim(
        "Branch captures are append-only. Push from a non-trunk branch "
        "and open a review in Spec Cloud to merge into trunk's prompts."
    )


# ---------------------------------------------------------------------------
# `prompts validate`
# ---------------------------------------------------------------------------


@prompts_group.command("validate")
@click.argument(
    "paths",
    nargs=-1,
    type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--strict-unknown",
    is_flag=True,
    help="Treat unknown tool_call names as errors (default: warn).",
)
def validate_cmd(paths: tuple[Path, ...], strict_unknown: bool) -> None:
    """Validate `.prompts` files against the schema. Exit 0 clean, 1 on error."""
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    if not paths:
        prompts_dir = _prompts_dir(root)
        if not prompts_dir.exists():
            dim(f"No .prompts files found under {PROMPTS_DIRNAME}/.")
            return
        target_paths: list[Path] = sorted(prompts_dir.glob("*.prompts"))
    else:
        target_paths = list(paths)

    if not target_paths:
        dim("No .prompts files to validate.")
        return

    n_ok = 0
    n_err = 0
    n_warn = 0

    from ..prompts.tools import ALLOWED_TOOL_NAMES

    for path in target_paths:
        try:
            pf = read_prompts_file(path)
        except PromptSchemaError as e:
            reject(str(e))
            n_err += 1
            continue

        warnings: list[str] = []
        for s_idx, session in enumerate(pf.sessions):
            for i, turn in enumerate(session.turns):
                for j, call in enumerate(turn.tool_calls):
                    if call.name not in ALLOWED_TOOL_NAMES:
                        warnings.append(
                            f"  sessions[{s_idx}].turns[{i}].tool_calls[{j}].name "
                            f"= `{call.name}` is not on the allowlist"
                        )
        if warnings and strict_unknown:
            reject(f"{path.name}: unknown tool name(s)")
            for w in warnings:
                reject(w)
            n_err += 1
            continue
        if warnings:
            dim(f"{path.name}: {len(warnings)} warning(s)")
            for w in warnings:
                dim(w)
            n_warn += len(warnings)

        n_ok += 1

    dim(
        f"checked {len(target_paths)} file(s) · {n_ok} ok · "
        f"{n_err} error(s) · {n_warn} warning(s)"
    )
    if n_err:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# `prompts simulate` — stub for v0.1
# ---------------------------------------------------------------------------


@prompts_group.command("simulate")
@click.option("--session", "session_ref", default=None, help="Session id or .prompts path.")
@click.option("--up-to-turn", type=int, default=None, help="Stop after turn N.")
@click.option("--model", default=None, help="Override compiler.model.")
@click.option(
    "--record", is_flag=True, help="POST a simulation record to Spec Cloud."
)
@click.option("--no-tools", is_flag=True, help="Refuse all tool calls — pure text reply.")
@click.option("--dry-run", is_flag=True, help="Show the plan, don't call the model.")
def simulate_cmd(
    session_ref: str | None,
    up_to_turn: int | None,
    model: str | None,
    record: bool,
    no_tools: bool,
    dry_run: bool,
) -> None:
    """Replay a session through the compiler in a read-only sandbox.

    Not implemented in v0.1 — this command is the contract surface. The
    implementation lands alongside the compiler's read-only tool profile,
    which is tracked as a separate piece of work. Calling it today prints
    the contract and exits non-zero so scripts fail loudly rather than
    silently succeeding.
    """
    info("spec prompts simulate — contract-only in v0.1.")
    info("")
    info("When implemented, this command will:")
    info("  1. Load the .prompts file that contains the session named by --session.")
    info("  2. Slice turns[0..up_to_turn] of that session and replay them.")
    info("  3. Run with a read-only tool profile (Read/Grep/Glob only).")
    info("  4. Write the simulated response to .spec/simulations/ (gitignored).")
    info("  5. Never overwrite the original .prompts file.")
    info("")
    info("See spec-cli/docs/prompt-format.md for the full contract.")
    raise SystemExit(2)


# ---------------------------------------------------------------------------
# `prompts submit` — promote a captured/authored prompt into review
# ---------------------------------------------------------------------------


def _resolve_prompt_arg(bundle_root: Path, arg: str) -> Path:
    """Turn a user-supplied path into an absolute prompt-file path.

    Accepts either:
      - an absolute or CWD-relative path (standard CLI convenience), or
      - a bundle-relative POSIX path like `prompts/captured/X.prompts`.

    Fails loudly if the result isn't under `prompts/` — we don't want
    `submit` to scoop up a random file on disk.
    """
    p = Path(arg)
    if not p.is_absolute():
        candidate = (bundle_root / arg).resolve()
        if candidate.is_file():
            p = candidate
        else:
            p = p.resolve()
    else:
        p = p.resolve()

    if not p.is_file():
        raise click.BadParameter(f"no such file: {arg}")
    if p.suffix.lower() != ".prompts":
        raise click.BadParameter(f"not a .prompts file: {arg}")

    try:
        rel = rel_posix(bundle_root, p)
    except ValueError as e:
        raise click.BadParameter(
            f"{arg} is outside the bundle root {bundle_root}"
        ) from e
    if not rel.startswith(PROMPTS_DIRNAME + "/") and rel != PROMPTS_DIRNAME:
        raise click.BadParameter(
            f"{arg} is not inside `{PROMPTS_DIRNAME}/`; only prompt files can be submitted"
        )
    return p


@prompts_group.command("submit")
@click.argument("paths", nargs=-1, required=False, type=click.Path())
@click.option(
    "--all-captured",
    is_flag=True,
    help=(
        "Submit every file currently under `prompts/captured/`. Useful when "
        "you want to batch-promote a commit's worth of auto-captured scrollback."
    ),
)
def submit_cmd(paths: tuple[str, ...], all_captured: bool) -> None:
    """Move prompts into `prompts/curated/_pending/` for reviewer sign-off.

    Submission is a pure filesystem move. The file ends up in the pending
    bucket with the same basename. It stops being advisory context (it
    leaves `captured/`) and it's not yet authoritative (it isn't in
    `curated/` until a reviewer accepts it).

    Expected shape of a PR:

        git add prompts/
        git commit -m "prompts: submit <name> for review"
        git push

    The reviewer then checks out the PR and runs `spec prompts review`.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    if not paths and not all_captured:
        fatal(
            "Pass one or more paths, or `--all-captured` to submit everything "
            "under `prompts/captured/`."
        )
        return

    sources: list[Path] = []
    if all_captured:
        cdir = captured_dir(root)
        if cdir.is_dir():
            sources.extend(sorted(p for p in cdir.glob("*.prompts") if p.is_file()))
    for arg in paths:
        sources.append(_resolve_prompt_arg(root, arg))

    if not sources:
        dim("Nothing to submit.")
        return

    pdir = pending_dir(root)
    pdir.mkdir(parents=True, exist_ok=True)

    moved: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    seen: set[Path] = set()

    for src in sources:
        if src in seen:
            continue
        seen.add(src)

        # Validate the file parses before we shuffle it around — a broken
        # prompt has no business entering review.
        try:
            read_prompts_file(src)
        except PromptSchemaError as e:
            skipped.append((rel_posix(root, src), f"schema error: {e}"))
            continue

        src_tier = classify_tier(rel_posix(root, src))
        if src_tier == Tier.PENDING:
            skipped.append((rel_posix(root, src), "already pending"))
            continue
        if src_tier == Tier.CURATED:
            skipped.append((rel_posix(root, src), "already curated; nothing to review"))
            continue

        dest = pdir / src.name
        if dest.exists():
            skipped.append(
                (rel_posix(root, src), f"would overwrite existing pending {dest.name}")
            )
            continue

        shutil.move(str(src), str(dest))
        moved.append((rel_posix(root, src), rel_posix(root, dest)))

    for src_rel, reason in skipped:
        reject(f"{src_rel} — {reason}")

    for src_rel, dest_rel in moved:
        ok(f"submitted [bold]{src_rel}[/] → {dest_rel}")

    if moved:
        pointer("next", "git add prompts/ && git commit && git push, then request review")
    if skipped and not moved:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# `prompts review` — interactive accept/reject of pending prompts
# ---------------------------------------------------------------------------


def _render_session_summary(session: Session) -> list[str]:
    """Human-skimmable summary of a single session for review.

    We deliberately do NOT dump tool calls by default — a reviewer cares
    about intent (user turns) and the author's own framing (title /
    summary / lesson). The file itself is always one `cat` away if they
    want the raw TOML.
    """
    lines: list[str] = []
    header = f"session {session.id[:12]}… · source={session.source}"
    if session.model:
        header += f" · model={session.model}"
    if session.outcome:
        header += f" · outcome={session.outcome}"
    lines.append(header)
    if session.title:
        lines.append(f"  title:   {session.title}")
    if session.summary:
        short = session.summary.strip().splitlines()[0][:160]
        lines.append(f"  summary: {short}")
    if session.lesson:
        lines.append(f"  lesson:  {session.lesson.strip()[:160]}")

    user_turns = [t for t in session.turns if t.role == "user" and t.text]
    if user_turns:
        lines.append(f"  user turns ({len(user_turns)}):")
        for i, t in enumerate(user_turns[:3]):
            snippet = t.text.strip().splitlines()[0][:140] if t.text else ""
            lines.append(f"    [{i}] {snippet}")
        if len(user_turns) > 3:
            lines.append(f"    … +{len(user_turns) - 3} more")

    tool_names: list[str] = []
    for t in session.turns:
        for c in t.tool_calls:
            tool_names.append(c.name)
    if tool_names:
        # Collapsed: just counts, not args. Args belong in the raw file.
        counts: dict[str, int] = {}
        for n in tool_names:
            counts[n] = counts.get(n, 0) + 1
        parts = [f"{n}×{counts[n]}" for n in sorted(counts)]
        lines.append(f"  tool calls: {', '.join(parts)}")

    return lines


def _render_pending_for_review(path: Path) -> str:
    try:
        pf = read_prompts_file(path)
    except PromptSchemaError as e:
        return f"  [schema error: {e}]"
    lines: list[str] = []
    lines.append(
        f"  commit: branch={pf.commit.branch} · author={pf.commit.author_name}"
    )
    for s in pf.sessions:
        lines.append("")
        lines.extend(_render_session_summary(s))
    return "\n".join(lines)


@prompts_group.command("review")
@click.option(
    "--accept",
    "accept_paths",
    multiple=True,
    type=click.Path(),
    help="Non-interactive: accept these specific pending files.",
)
@click.option(
    "--reject",
    "reject_paths",
    multiple=True,
    type=click.Path(),
    help="Non-interactive: reject these specific pending files.",
)
@click.option(
    "--yes-all",
    is_flag=True,
    help="Accept every pending file without prompting. Use only when you've read them elsewhere.",
)
def review_cmd(
    accept_paths: tuple[str, ...],
    reject_paths: tuple[str, ...],
    yes_all: bool,
) -> None:
    """Walk pending prompts, render a summary, and accept or reject each.

    Accept → move from `prompts/curated/_pending/` to `prompts/curated/`.
    Reject → delete from the worktree.

    Review only touches files; it never runs `git commit`. The reviewer
    stages and commits the resulting changes themselves so the audit lives
    in normal git history:

        git add prompts/
        git commit -m "prompts: review <names>"
        git push

    After the push, the PR's required `spec prompts check --ci` turns
    green (no `_pending/` files remain) and the PR becomes mergeable.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    pending = list(iter_pending(root))
    if not pending:
        dim(f"No pending prompts under `{PROMPTS_DIRNAME}/{PROMPTS_CURATED_DIRNAME}/{PROMPTS_PENDING_DIRNAME}/`.")
        return

    # Resolve any explicit --accept / --reject paths into our pending set.
    explicit_accept: set[Path] = set()
    explicit_reject: set[Path] = set()
    for arg in accept_paths:
        p = _resolve_prompt_arg(root, arg)
        explicit_accept.add(p)
    for arg in reject_paths:
        p = _resolve_prompt_arg(root, arg)
        explicit_reject.add(p)
    overlap = explicit_accept & explicit_reject
    if overlap:
        fatal(
            "A file cannot be both --accept and --reject: "
            + ", ".join(sorted(rel_posix(root, p) for p in overlap))
        )
        return

    cdir = curated_dir(root)
    cdir.mkdir(parents=True, exist_ok=True)

    accepted: list[str] = []
    rejected: list[str] = []
    skipped: list[str] = []

    console.print(
        f"[sf.label]prompts review[/] [sf.muted]· "
        f"{len(pending)} pending file(s)[/]"
    )

    for tp in pending:
        rel = tp.rel
        console.print("")
        console.print(f"[sf.label]{rel}[/]")
        console.print(_render_pending_for_review(tp.abs_path))

        decision: str
        if tp.abs_path in explicit_accept:
            decision = "accept"
        elif tp.abs_path in explicit_reject:
            decision = "reject"
        elif yes_all:
            decision = "accept"
        else:
            choice = click.prompt(
                "  [a]ccept / [r]eject / [s]kip",
                type=click.Choice(["a", "r", "s"], case_sensitive=False),
                default="s",
                show_default=True,
            ).lower()
            decision = {"a": "accept", "r": "reject", "s": "skip"}[choice]

        if decision == "accept":
            dest = cdir / tp.abs_path.name
            if dest.exists():
                reject(
                    f"cannot accept — {rel_posix(root, dest)} already exists. "
                    "Rename the pending file or remove the conflict."
                )
                skipped.append(rel)
                continue
            shutil.move(str(tp.abs_path), str(dest))
            accepted.append(rel_posix(root, dest))
            ok(f"accepted → {rel_posix(root, dest)}")
        elif decision == "reject":
            tp.abs_path.unlink()
            rejected.append(rel)
            reject(f"rejected · deleted {rel}")
        else:
            skipped.append(rel)
            dim(f"skipped {rel}")

    console.print("")
    dim(
        f"review complete · {len(accepted)} accepted · "
        f"{len(rejected)} rejected · {len(skipped)} skipped"
    )
    if accepted or rejected:
        pointer(
            "next",
            "git add prompts/ && git commit -m 'prompts: review' && git push",
        )
    if skipped:
        warn(
            f"{len(skipped)} file(s) still pending. Merge will remain blocked "
            "until every pending prompt is accepted or rejected."
        )


# ---------------------------------------------------------------------------
# `prompts check` — CI-friendly gate on pending files
# ---------------------------------------------------------------------------


@prompts_group.command("check")
@click.option(
    "--ci",
    "ci_mode",
    is_flag=True,
    help="Quiet output suitable for required-status-check logs.",
)
def check_cmd(ci_mode: bool) -> None:
    """Exit non-zero if any prompt is awaiting review.

    Meant as a GitHub Actions required status check:

        - name: spec prompts check
          run: spec prompts check --ci

    Combined with branch protection ("Require status checks to pass"), this
    refuses to merge any PR that still has files under
    `prompts/curated/_pending/`. Rejection during review deletes the
    pending file, so rejected prompts never land on the default branch —
    the code that reaches `main` is exactly the code the reviewer
    accepted.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    pending = list(iter_pending(root))
    if not pending:
        if ci_mode:
            # One line, no ANSI noise. Good for CI logs.
            click.echo("spec prompts: ok · 0 pending")
        else:
            ok("0 pending prompts · ready to merge")
        return

    if ci_mode:
        click.echo(f"spec prompts: FAIL · {len(pending)} pending")
        for tp in pending:
            click.echo(f"  pending: {tp.rel}")
    else:
        reject(f"{len(pending)} prompt(s) awaiting review:")
        for tp in pending:
            console.print(f"  [sf.muted]·[/] {tp.rel}")
        dim("Run `spec prompts review` locally on this branch.")
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# `prompts status` — at-a-glance tier counts
# ---------------------------------------------------------------------------


@prompts_group.command("status")
def status_cmd() -> None:
    """Summarize the prompt tiers in the bundle."""
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    counts = count_tiers(root)
    console.print(f"[sf.label]prompts[/] [sf.muted]· {counts.total} file(s)[/]")
    console.print(f"  curated:  {counts.curated}")
    console.print(f"  captured: {counts.captured}")
    if counts.legacy:
        console.print(
            f"  legacy:   {counts.legacy}   [sf.muted](under prompts/ — "
            "treated as curated)[/]"
        )
    if counts.pending:
        console.print(f"  [sf.warn]pending:  {counts.pending}[/]   (awaiting review)")
    else:
        console.print("  pending:  0")
