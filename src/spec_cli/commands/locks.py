"""``spec locks`` — one decision over every coordination signal.

``spec locks check`` combines Cloud-backed task claims, dirty-tree presence,
and machine-local active edits. Its tri-state exit contract is 0 clear, 2
conflict, and 3 unknown; unavailable telemetry never masquerades as safety.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from ..config import BundleNotFoundError, find_bundle_root
from ..realtime.active_edits import (
    DEFAULT_LOCK_TTL_SECS,
    ActiveEditsStore,
)
from ..realtime.conflicts import (
    ConflictAssessment,
    assess_path_conflict,
    resolve_coordination_path,
)
from ..realtime.presence_mirror import read_team_presence
from ..realtime.team_editing_brief import (
    DEFAULT_LOCKS_MIRROR_STALE_SECS,
    TEAM_EDITING_BRIEF_FILENAME,
    _compute_pull_alerts,
    team_presence_mirror_stale,
)
from ..ui import console, dim, fatal, ok, warn


def _locks_max_mirror_age_secs() -> float:
    raw = os.environ.get("SPEC_LOCKS_MAX_MIRROR_AGE_SECS", "").strip()
    if not raw:
        return DEFAULT_LOCKS_MIRROR_STALE_SECS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_LOCKS_MIRROR_STALE_SECS


def _render_assessment(assessment: ConflictAssessment) -> None:
    if assessment.state == "unknown":
        warn(
            f"⚠ {assessment.path} — coordination state unknown "
            f"({assessment.reason or 'unavailable'}). Start or repair `spec watch` "
            "before treating the path as clear."
        )
        return
    if assessment.state == "clear":
        if assessment.pull_alerts:
            warn(
                "⚠ pull needed before editing — "
                + ", ".join(
                    f"@{row['handle']} on {row['branch']} at {row['short_commit']}"
                    for row in assessment.pull_alerts
                )
            )
        ok(f"clear: no active claim or teammate edit on {assessment.path}")
        return

    lines: list[str] = []
    for holder in assessment.holders:
        kind = holder.get("kind") or "dirty_tree"
        handle = holder.get("handle") or holder.get("name") or "(unknown)"
        if kind == "task_claim":
            lines.append(
                f"  · {handle} ({holder.get('agent') or 'agent'}, "
                f"session {holder.get('session_id') or '-'}) — "
                f"{holder.get('objective') or 'active task claim'}"
            )
        elif kind == "active_edit":
            lines.append(
                f"  · {handle} (session {holder.get('session_id') or '-'}, "
                f"intent {holder.get('intent') or '-'})"
            )
        else:
            added = int(holder.get("lines_added") or 0)
            removed = int(holder.get("lines_removed") or 0)
            lines.append(f"  · @{handle} dirty tree (+{added}/-{removed})")
    warn(
        f"⚠ {assessment.path} — active coordination conflict:\n"
        + "\n".join(lines)
        + "\n  Coordinate or use an isolated worktree before editing."
    )


@click.group("locks")
def locks_group() -> None:
    """Assess and manage Spec Live edit coordination."""


@locks_group.command("check")
@click.argument("path", type=str)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    help="Suppress stdout. Exit code is the contract.",
)
@click.option(
    "--include-self",
    is_flag=True,
    help="Also treat overlap with your own dirty files as a conflict.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit machine-readable JSON instead of a rendered warning.",
)
@click.option(
    "--agent",
    "caller_agent",
    type=str,
    default=None,
    help="Calling agent id; pair with --session to ignore its own lease.",
)
@click.option(
    "--session",
    "caller_session_id",
    type=str,
    default=None,
    help="Calling agent session id; pair with --agent.",
)
def locks_check_cmd(
    path: str,
    quiet: bool,
    include_self: bool,
    as_json: bool,
    caller_agent: str | None,
    caller_session_id: str | None,
) -> None:
    """Assess one path using task claims, presence, and local leases.

    Exit **0** only for a fresh, clear assessment; **2** for a conflict;
    **3** when coordination health is unknown. This keeps unavailable or
    stale telemetry from masquerading as proof that a path is safe.
    """
    caller_session_id = caller_session_id or os.environ.get(
        "SPEC_AGENT_SESSION_ID"
    )
    caller_agent = caller_agent or os.environ.get("SPEC_AGENT_SOURCE")
    if caller_session_id is None:
        caller_session_id = os.environ.get("CODEX_THREAD_ID") or os.environ.get(
            "CODEX_SESSION_ID"
        )
        if caller_session_id:
            caller_agent = caller_agent or "codex"

    root, rel = resolve_coordination_path(path)
    if root is None or rel is None:
        payload = {
            "state": "unknown",
            "clear": False,
            "path": path,
            "holders": [],
            "pull_alerts": [],
            "reason": "outside_workspace",
        }
        if as_json:
            click.echo(json.dumps(payload))
        elif not quiet:
            warn(f"⚠ {path} — coordination state unknown (outside a Git workspace).")
        sys.exit(3)

    assessment = assess_path_conflict(
        root,
        rel,
        include_self_dirty=include_self,
        caller_agent=caller_agent,
        caller_session_id=caller_session_id,
        max_presence_age_secs=_locks_max_mirror_age_secs(),
    )
    if as_json:
        click.echo(json.dumps(assessment.to_json()))
    elif not quiet:
        _render_assessment(assessment)
    sys.exit({"clear": 0, "conflict": 2, "unknown": 3}[assessment.state])


@locks_group.command("pull-status")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit machine-readable JSON. Default is a human summary.",
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    help="Suppress stdout. Exit code is the contract.",
)
def locks_pull_status_cmd(as_json: bool, quiet: bool) -> None:
    """Surface "teammates pushed, you should pull" alerts from the live mirror.

    Unlike the tri-state pre-edit check, this advisory command fails open when
    its presence mirror is missing or stale (exit 0). Exit **2** when at least
    one teammate is on the same
    branch with a different ``head_commit`` — the canonical "git
    pull before you keep editing" signal — and the mirror is fresh.

    Designed as a cheap, parseable pull reminder; it is not a path-clearance
    contract.
    """
    max_age = _locks_max_mirror_age_secs()
    try:
        root = find_bundle_root()
    except BundleNotFoundError:
        if as_json:
            click.echo(
                json.dumps({"clear": True, "reason": "not_in_bundle", "alerts": []})
            )
        sys.exit(0)

    body = read_team_presence(root)
    if body is None:
        if as_json:
            click.echo(
                json.dumps({"clear": True, "reason": "no_live_data", "alerts": []})
            )
        sys.exit(0)

    if team_presence_mirror_stale(body, max_age_secs=max_age):
        if as_json:
            click.echo(
                json.dumps({"clear": True, "reason": "stale_mirror", "alerts": []})
            )
        elif not quiet:
            dim(
                "locks: team-presence mirror is stale — treating as clear "
                "(start `spec watch` for live data)."
            )
        sys.exit(0)

    alerts = _compute_pull_alerts(body)
    if not alerts:
        if as_json:
            click.echo(json.dumps({"clear": True, "alerts": []}))
        elif not quiet:
            ok("clear: no teammate is ahead of your branch")
        sys.exit(0)

    if as_json:
        click.echo(json.dumps({"clear": False, "alerts": alerts}))
    elif not quiet:
        bullets = "\n".join(
            f"  · @{a['handle']} on `{a['branch']}` at `{a['short_commit']}` "
            f"(you: `{a['self_short']}`)"
            for a in alerts
        )
        warn(
            "⚠ pull needed — teammate(s) pushed commits ahead of your branch:\n"
            + bullets
            + "\n  Run `git pull` before continuing to edit."
        )
    sys.exit(2)


@locks_group.command("brief-path")
def locks_brief_path_cmd() -> None:
    """Print the absolute path to ``.spec/team-editing-brief.md``."""
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return
    p = (root / ".spec" / TEAM_EDITING_BRIEF_FILENAME).resolve()
    click.echo(str(p))


@locks_group.command("show-brief")
def locks_show_brief_cmd() -> None:
    """Display ``.spec/team-editing-brief.md`` when it exists."""
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return
    p = root / ".spec" / TEAM_EDITING_BRIEF_FILENAME
    if not p.is_file():
        dim(f"no {TEAM_EDITING_BRIEF_FILENAME} yet — run `spec watch`.")
        return
    console.print(p.read_text(encoding="utf-8"))


# ── single-user multi-agent locks ──────────────────────────────────


@locks_group.command("acquire")
@click.argument("paths", nargs=-1, required=True)
@click.option(
    "--agent",
    type=str,
    default="manual",
    show_default=True,
    help=(
        "Identifier for the agent taking the lock. Known agents that "
        "get coloured / labelled output: claude_code, cursor, codex, "
        "compress, manual. Free-form strings are accepted; stick to one per "
        "tool so the brief stays readable."
    ),
)
@click.option(
    "--session",
    "session_id",
    type=str,
    default=None,
    help=(
        "Stable session id for the calling agent (Claude Code session "
        "id, Cursor composer id, Codex thread id). Used so a single "
        "long-running agent loop renews its own lock instead of "
        "conflicting with itself."
    ),
)
@click.option(
    "--ttl",
    "ttl_secs",
    type=float,
    default=float(DEFAULT_LOCK_TTL_SECS),
    show_default=True,
    help=(
        "Lock duration in seconds. Capped at 3600. A crashed agent "
        "never holds a lock past the TTL — peers see the lock "
        "expire automatically."
    ),
)
@click.option(
    "--intent",
    type=str,
    default=None,
    help="Short tag describing the lock (e.g. tool name `Edit`).",
)
@click.option(
    "--note",
    type=str,
    default=None,
    help="Optional free-form note shown in the brief and `list` output.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit machine-readable JSON.",
)
@click.option(
    "--block",
    is_flag=True,
    help=(
        "Exit non-zero when another agent already holds an overlapping "
        "lock. Default is warn-only: the lock is granted and the "
        "conflict surfaced for the caller to decide on."
    ),
)
def locks_acquire_cmd(
    paths: tuple[str, ...],
    agent: str,
    session_id: str | None,
    ttl_secs: float,
    intent: str | None,
    note: str | None,
    as_json: bool,
    block: bool,
) -> None:
    """Take a lock on one or more files for this agent.

    The lock lives in ``~/.spec/active-edits.json`` (local-only, never
    broadcast) and is namespaced by this bundle root. Use it from a
    PreToolUse hook so other agents on
    the same machine see your edit in flight before they try the
    same file. Same agent + session re-acquires extend the lock
    (renewal); cross-agent overlaps return as ``conflicts``.

    Exit codes:

    * ``0`` — acquired, no conflicts.
    * ``0`` — acquired, conflicts present, ``--block`` not set.
    * ``2`` — acquired, conflicts present, ``--block`` set.

    The lock id printed to stdout (or returned in JSON) is the
    handle for ``spec locks release``.
    """
    # Resolve once from the first target. A generated coordination mirror or
    # Git worktree is a valid local lease namespace even when the repository
    # is not itself a compile bundle.
    root, first_rel = resolve_coordination_path(paths[0])
    if root is None or first_rel is None:
        if as_json:
            click.echo(
                json.dumps({"acquired": False, "reason": "outside_workspace"})
            )
        else:
            fatal("path is outside a Git workspace")
        sys.exit(1)

    rels: list[str] = []
    for raw in paths:
        candidate_root, rel = resolve_coordination_path(raw)
        if rel is None or candidate_root != root:
            if as_json:
                click.echo(
                    json.dumps(
                        {
                            "acquired": False,
                            "reason": "path_outside_workspace_scope",
                            "path": raw,
                        }
                    )
                )
            else:
                fatal(f"path is outside the coordination scope: {raw}")
            sys.exit(1)
        rels.append(rel)

    store = ActiveEditsStore(root)
    lock, conflicts = store.acquire(
        rels,
        agent=agent,
        session_id=session_id,
        ttl_secs=ttl_secs,
        intent=intent,
        note=note,
    )

    conflict_payload = [
        {
            "lock_id": c.lock.id,
            "bundle_root": c.lock.bundle_root,
            "agent": c.lock.agent,
            "session_id": c.lock.session_id,
            "intent": c.lock.intent,
            "pid": c.lock.pid,
            "expires_at": c.lock.expires_at.isoformat(),
            "overlapping_paths": list(c.overlapping_paths),
        }
        for c in conflicts
    ]

    if as_json:
        click.echo(
            json.dumps(
                {
                    "acquired": True,
                    "lock_id": lock.id,
                    "bundle_root": lock.bundle_root,
                    "paths": list(lock.paths),
                    "agent": lock.agent,
                    "session_id": lock.session_id,
                    "expires_at": lock.expires_at.isoformat(),
                    "conflicts": conflict_payload,
                }
            )
        )
    else:
        ok(
            f"acquired lock {lock.id[:8]} on "
            f"{', '.join(f'`{p}`' for p in lock.paths)} "
            f"(agent={lock.agent}, expires at {lock.expires_at.isoformat()})"
        )
        if conflicts:
            bullet = "\n".join(
                f"  · {c.lock.agent} "
                f"(session={c.lock.session_id or '-'}, "
                f"pid={c.lock.pid}): "
                f"{', '.join(c.overlapping_paths)} "
                f"(intent={c.lock.intent or '-'})"
                for c in conflicts
            )
            warn(
                "⚠ another agent already holds overlapping locks:\n" + bullet
            )
    if conflicts and block:
        sys.exit(2)
    sys.exit(0)


@locks_group.command("release")
@click.argument("lock_id", type=str)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit machine-readable JSON.",
)
def locks_release_cmd(lock_id: str, as_json: bool) -> None:
    """Drop a previously-acquired lock by id.

    Exit ``0`` whether the lock existed or not — the typical caller is
    a PostToolUse hook releasing the lock the matching PreToolUse hook
    took, and an already-expired lock should never look like an error.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError:
        # IDs are globally unique, so release remains useful from outside
        # the original bundle (for example during machine cleanup).
        root = Path.cwd()
    store = ActiveEditsStore(root)
    removed = store.release(lock_id)
    if as_json:
        click.echo(json.dumps({"released": bool(removed), "lock_id": lock_id}))
    else:
        if removed:
            ok(f"released lock {lock_id[:8]}")
        else:
            dim(f"no active lock found for id {lock_id[:8]} (already expired?)")
    sys.exit(0)


@locks_group.command("list")
@click.option(
    "--include-expired",
    is_flag=True,
    help="Also show expired locks (useful when debugging stale state).",
)
@click.option(
    "--all",
    "all_bundles",
    is_flag=True,
    help="Show locks for every Spec bundle in the machine-wide registry.",
)
@click.option(
    "--agent",
    type=str,
    default=None,
    help="Filter to one agent identifier.",
)
@click.option(
    "--session",
    "session_id",
    type=str,
    default=None,
    help="Filter to one session id.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit machine-readable JSON.",
)
def locks_list_cmd(
    include_expired: bool,
    all_bundles: bool,
    agent: str | None,
    session_id: str | None,
    as_json: bool,
) -> None:
    """Show active edit locks for this bundle or the whole machine.

    These are local-only and live in ``~/.spec/active-edits.json``. A
    fresh ``spec watch`` doesn't need to be running for this — the
    file is written synchronously by each ``spec locks acquire`` and
    ``spec locks release`` call.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError:
        if not all_bundles:
            if as_json:
                click.echo(json.dumps({"locks": [], "reason": "not_in_bundle"}))
            else:
                dim("not inside a Spec bundle; pass `--all` for machine locks.")
            sys.exit(0)
        root = Path.cwd()

    store = ActiveEditsStore(root)
    if all_bundles:
        locks = store.list_all(include_expired=include_expired)
    else:
        locks = store.list(include_expired=include_expired)
    if agent:
        locks = [
            lk for lk in locks
            if (lk.agent or "").strip().lower() == agent.strip().lower()
        ]
    if session_id is not None:
        locks = [lk for lk in locks if (lk.session_id or "") == session_id]

    if as_json:
        click.echo(
            json.dumps(
                {
                    "locks": [
                        {
                            "id": lk.id,
                            "bundle_root": lk.bundle_root,
                            "paths": list(lk.paths),
                            "agent": lk.agent,
                            "session_id": lk.session_id,
                            "pid": lk.pid,
                            "host": lk.host,
                            "started_at": lk.started_at.isoformat(),
                            "expires_at": lk.expires_at.isoformat(),
                            "intent": lk.intent,
                            "note": lk.note,
                            "expired": lk.is_expired(),
                        }
                        for lk in locks
                    ]
                }
            )
        )
        sys.exit(0)

    if not locks:
        ok("no active edit locks")
        sys.exit(0)
    for lk in locks:
        expired_tag = " [EXPIRED]" if lk.is_expired() else ""
        paths_fmt = ", ".join(f"`{p}`" for p in lk.paths)
        console.print(
            f"[sf.label]{lk.id[:8]}[/]{expired_tag} "
            f"[sf.muted]·[/] [bold]{lk.agent}[/] "
            f"[sf.muted]· session[/] {lk.session_id or '-'} "
            f"[sf.muted]· pid[/] {lk.pid} "
            f"[sf.muted]· expires[/] {lk.expires_at.isoformat()}"
        )
        if all_bundles:
            console.print(f"  [sf.muted]bundle:[/] {lk.bundle_root}")
        console.print(f"  [sf.muted]paths:[/] {paths_fmt}")
        if lk.intent:
            console.print(f"  [sf.muted]intent:[/] {lk.intent}")
        if lk.note:
            console.print(f"  [sf.muted]note:[/] {lk.note}")
    sys.exit(0)


@locks_group.command("prune")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit machine-readable JSON.",
)
def locks_prune_cmd(as_json: bool) -> None:
    """Remove expired locks from the machine-wide registry.

    ``list`` and ``check`` already filter expired locks at read
    time, so calling ``prune`` is housekeeping (keeps the file
    small + lets external inspectors see a tidy view). Exit ``0``
    even when nothing was removed.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError:
        root = Path.cwd()
    store = ActiveEditsStore(root)
    removed = store.prune()
    if as_json:
        click.echo(json.dumps({"pruned": int(removed)}))
    else:
        ok(f"pruned {removed} expired lock(s)")
    sys.exit(0)
