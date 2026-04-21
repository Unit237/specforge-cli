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
from ..git import read_git_context
from ..prompts import (
    CommitMeta,
    PromptSchemaError,
    PromptsFile,
    Session,
    read_prompts_file,
)
from ..prompts.render import prompts_filename, render_prompts_file
from ..prompts.tiers import (
    Tier,
    captured_dir,
    classify_tier,
    count_tiers,
    curated_dir,
    iter_all_prompts,
    iter_pending,
    pending_dir,
)
from ..sources import (
    ClaudeCodeError,
    claude_code_project_dir,
    read_claude_code_sessions,
)
from ..sources.claude_code import claude_code_store_root
from ..stage import rel_posix
from ..ui import console, dim, fatal, info, ok, pointer, reject, warn


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


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
    type=click.Choice(["claude_code", "all"], case_sensitive=False),
    default="all",
    help="Restrict capture to one source. Currently only claude_code is implemented.",
)
@click.option(
    "--since",
    default=None,
    help="Only sessions started after this ISO 8601 timestamp (e.g. 2026-04-01T00:00:00Z).",
)
@click.option(
    "--verbose",
    "verbose_capture",
    is_flag=True,
    help=(
        "Capture full assistant text in `text` fields (off by default). "
        "Resulting sessions are marked `verbose = true` per the schema."
    ),
)
@click.option("--dry-run", is_flag=True, help="Print counts, don't write any file.")
def capture_cmd(
    source: str,
    since: str | None,
    verbose_capture: bool,
    dry_run: bool,
) -> None:
    """Snapshot every new conversational session into one `.prompts` file.

    Writes `prompts/<UTC-timestamp>.prompts` at the bundle root, containing
    a `[commit]` block (from your git context) plus one `[[sessions]]` block
    per discovered session. Run after each commit, or wire into a post-commit
    hook; sessions already captured in any prior `.prompts` file are skipped.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    since_dt = _parse_since(since)

    # Prove the Claude Code store exists — users who've never run Claude
    # Code get a friendly pointer, not a cryptic empty result.
    store_root = claude_code_store_root()
    if not store_root.exists():
        dim(f"Claude Code store not found at {store_root}.")
        info("Install Claude Code and start a session, then re-run `spec prompts capture`.")
        info("  https://claude.ai/code")
        return

    project_dir = claude_code_project_dir(root)
    if not project_dir.exists():
        dim("No Claude Code sessions recorded for this bundle yet.")
        dim(f"(looked for {project_dir.name} under {store_root})")
        return

    # Guard against the one-source switch. Cursor capture is not in v0.1.
    if source.lower() not in ("claude_code", "all"):
        fatal(f"Unknown source `{source}`.")
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
    try:
        for session in read_claude_code_sessions(
            root, since=since_dt, verbose=verbose_capture
        ):
            if session.id in already_captured:
                continue
            discovered.append(session)
    except ClaudeCodeError as e:
        fatal(str(e))
        return

    if not discovered:
        dim("No new sessions to capture.")
        return

    # Tag each session with who drove it. With git identity alone this is
    # a best guess; when credentials are linked to Cloud, the username is
    # written in `[commit].author_username` separately.
    for s in discovered:
        if s.operator is None:
            s.operator = author_email

    now = datetime.now(timezone.utc)
    pf = PromptsFile(
        commit=CommitMeta(
            branch=branch,
            author_name=author_name,
            author_email=author_email,
            committed_at=None,  # populated by the push flow / hook; unknown here
            message=None,
            author_username=None,
        ),
        sessions=discovered,
        edits=[],
    )

    try:
        body = render_prompts_file(pf)
    except PromptSchemaError as e:
        fatal(f"render failed: {e}")
        return

    target_dir = captured_dir(root)
    filename = prompts_filename(now)
    dest = target_dir / filename
    rel_dest = f"{PROMPTS_DIRNAME}/{PROMPTS_CAPTURED_DIRNAME}/{filename}"

    console.print(
        f"[sf.label]prompts capture[/] [sf.muted]· "
        f"{len(discovered)} new session(s) → {rel_dest}[/]"
    )
    if warn_non_git:
        dim(
            "Not a git worktree — writing `branch=detached` and "
            "`author=unknown` into [commit]. You'll want to hand-edit "
            "those before pushing."
        )

    if dry_run:
        dim("\n--dry-run: skipping write.")
        return

    if dest.exists():
        fatal(
            f"{dest.relative_to(root)} already exists. Two captures in the "
            "same second is unusual — wait a moment and re-run."
        )
        return

    target_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(body, encoding="utf-8")

    pointer("wrote", str(dest.relative_to(root)))
    dim(
        f"Captured prompts are advisory context. To promote one for review, "
        f"run `spec prompts submit {rel_dest}`."
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
