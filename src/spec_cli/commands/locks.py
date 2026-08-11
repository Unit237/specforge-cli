"""``spec locks`` — coordination checks using the team-presence mirror.

``spec locks check`` matches ``spec presence check`` exit codes (0 clear,
2 conflict) but **ignores a stale** ``.spec/team-presence.json`` (when
``updated_at`` is older than a few minutes, ``spec watch`` is probably
not running — we fail open instead of trusting zombie data).

``.spec/team-editing-brief.md`` is a plain-language sibling file updated
with the JSON; agents can read it directly.
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
    ActiveEditLock,
    ActiveEditsStore,
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


@click.group("locks")
def locks_group() -> None:
    """Edit coordination using the Spec Live presence mirror."""


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
def locks_check_cmd(path: str, quiet: bool, include_self: bool, as_json: bool) -> None:
    """Like ``spec presence check``, but ignores a stale presence mirror.

    Exit **0** when the mirror is missing, you're outside a bundle, or
    ``updated_at`` is older than ``SPEC_LOCKS_MAX_MIRROR_AGE_SECS``
    (default: same as ``DEFAULT_LOCKS_MIRROR_STALE_SECS`` — 15 minutes).

    Exit **2** when at least one teammate (non-self) has the path dirty
    and the mirror is fresh.
    """
    max_age = _locks_max_mirror_age_secs()
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        if not quiet and not as_json:
            dim(f"not in a Spec bundle ({e}); skipping locks check.")
        if as_json:
            click.echo(json.dumps({"clear": True, "reason": "not_in_bundle"}))
        sys.exit(0)

    rel = _bundle_relative_path(path, root)
    if rel is None:
        if as_json:
            click.echo(json.dumps({"clear": True, "reason": "outside_bundle"}))
        sys.exit(0)

    # Single-user, multi-agent locks live in a *different* machine file
    # (``~/.spec/active-edits.json``) and exist precisely to coordinate
    # between Claude Code, Cursor, Codex etc. running side-by-side
    # on one machine. We read them **first**, before consulting the
    # team-presence mirror, because the active-edits layer has no
    # dependency on ``spec watch`` running — a single dev with two
    # agents but no live daemon still needs check to fire.
    active_holders = _active_holders_for_path(root, rel)
    active_holder_payload = [
        _active_lock_to_holder(lock) for lock in active_holders
    ]

    body = read_team_presence(root)
    if body is None:
        # No team-presence mirror, but active-edits may still flag a
        # conflict on this machine. Surface them; otherwise emit the
        # original "no live data" clear response.
        if active_holder_payload:
            if as_json:
                click.echo(
                    json.dumps(
                        {
                            "clear": False,
                            "path": rel,
                            "holders": active_holder_payload,
                            "pull_alerts": [],
                            "reason": "no_team_presence_active_only",
                        }
                    )
                )
            elif not quiet:
                warn(
                    f"⚠ {rel} — one of your own agents is currently editing:\n"
                    + "\n".join(
                        f"  · {h.get('agent')} (session {h.get('session_id') or '-'}, "
                        f"intent {h.get('intent') or '-'})"
                        for h in active_holder_payload
                    )
                )
            sys.exit(2)
        if as_json:
            click.echo(json.dumps({"clear": True, "reason": "no_live_data"}))
        sys.exit(0)

    if team_presence_mirror_stale(body, max_age_secs=max_age):
        # Same exception as above: a stale team-presence mirror does
        # not invalidate the per-machine active-edits layer.
        if active_holder_payload:
            if as_json:
                click.echo(
                    json.dumps(
                        {
                            "clear": False,
                            "path": rel,
                            "holders": active_holder_payload,
                            "pull_alerts": [],
                            "reason": "stale_team_presence_active_only",
                        }
                    )
                )
            elif not quiet:
                warn(
                    f"⚠ {rel} — one of your own agents is currently editing:\n"
                    + "\n".join(
                        f"  · {h.get('agent')} (session {h.get('session_id') or '-'}, "
                        f"intent {h.get('intent') or '-'})"
                        for h in active_holder_payload
                    )
                )
            sys.exit(2)
        if as_json:
            click.echo(json.dumps({"clear": True, "reason": "stale_mirror"}))
        elif not quiet:
            dim(
                "locks: team-presence mirror is stale or undated — "
                "treating as clear (start `spec watch` for live data)."
            )
        sys.exit(0)

    # Pull-needed peers are always surfaced when the mirror is fresh —
    # regardless of whether the requested path overlaps with a teammate
    # edit. An AI IDE / hook reading this is about to write *anything*
    # into the working tree, and knowing the branch is behind a
    # teammate's just-pushed commit is exactly the moment to ``git
    # pull`` first.
    pull_alerts = _compute_pull_alerts(body)
    holders = _holders_for_path(body, rel, include_self=include_self)

    if active_holder_payload:
        # An active-edit lock is a conflict regardless of whether a
        # teammate is also dirty on the path — we always want the
        # exit code to reflect it.
        holders = holders + active_holder_payload

    if not holders:
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "clear": True,
                        "path": rel,
                        "holders": [],
                        "pull_alerts": pull_alerts,
                    }
                )
            )
        elif not quiet:
            if pull_alerts:
                warn(
                    "⚠ pull needed before editing — "
                    + ", ".join(
                        f"@{a['handle']} on {a['branch']} at {a['short_commit']}"
                        for a in pull_alerts
                    )
                    + f" (you: {pull_alerts[0]['self_short']}). "
                    "Run `git pull` first."
                )
            ok(f"clear: no teammate is editing {rel}")
        # ``clear`` (no path overlap) trumps "pull needed" in the exit
        # code so existing CI / hook integrations don't suddenly start
        # failing — pull-state is a hint, not a hard block.
        sys.exit(0)

    if as_json:
        click.echo(
            json.dumps(
                {
                    "clear": False,
                    "path": rel,
                    "holders": holders,
                    "pull_alerts": pull_alerts,
                }
            )
        )
    elif not quiet:
        bullet_lines = []
        for h in holders:
            handle = h.get("handle") or h.get("name") or "(unknown)"
            added = int(h.get("lines_added") or 0)
            removed = int(h.get("lines_removed") or 0)
            untracked = " (new file)" if h.get("untracked") else ""
            bullet_lines.append(
                f"  · @{handle} (+{added}/-{removed}){untracked}"
            )
        msg = (
            f"⚠ {rel} — teammate(s) may be editing (fresh mirror):\n"
            + "\n".join(bullet_lines)
            + "\n  Pull / coordinate before making conflicting changes."
        )
        if pull_alerts:
            msg += "\n  " + "; ".join(
                f"@{a['handle']} pushed to {a['branch']} ({a['short_commit']}) — git pull"
                for a in pull_alerts
            )
        warn(msg)
    sys.exit(2)


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

    Same staleness handling as ``spec locks check``: a missing /
    stale mirror means the watcher isn't running, so we fail open
    (exit 0). Exit **2** when at least one teammate is on the same
    branch with a different ``head_commit`` — the canonical "git
    pull before you keep editing" signal — and the mirror is fresh.

    Designed for AI IDE rules and pre-edit hooks: cheap, parseable,
    and matches the ``locks check`` exit-code contract.
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


def _bundle_relative_path(raw: str, bundle_root: Path) -> str | None:
    if not raw:
        return None
    p = Path(raw)
    if p.is_absolute():
        try:
            return str(p.resolve().relative_to(bundle_root.resolve()))
        except ValueError:
            return None
    candidate = (bundle_root / raw).resolve()
    try:
        return str(candidate.relative_to(bundle_root.resolve()))
    except ValueError:
        return None


def _holders_for_path(
    body: dict, rel_path: str, *, include_self: bool
) -> list[dict]:
    files_index = body.get("files_index")
    if not isinstance(files_index, dict):
        return []
    raw = files_index.get(rel_path)
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if not include_self and bool(entry.get("self") or False):
            continue
        out.append(entry)
    return out


def _active_holders_for_path(
    bundle_root: Path, rel_path: str
) -> list[ActiveEditLock]:
    """Locks from the local active-edits store covering ``rel_path``.

    Failures are swallowed: a malformed lock file should not block
    ``spec locks check``. The store handles the actual JSON read +
    parse gracefully; we just provide a thin "fail open" wrapper.
    """
    try:
        store = ActiveEditsStore(bundle_root)
        return store.holders_for(rel_path)
    except Exception:  # noqa: BLE001
        return []


def _active_lock_to_holder(lock: ActiveEditLock) -> dict:
    """Project an :class:`ActiveEditLock` into the same shape as the
    team-presence ``holders[]`` rows so JSON consumers can treat both
    sources uniformly. We add a ``kind`` field so a renderer that
    cares about the distinction (cross-machine teammate vs same-
    machine agent) can disambiguate.
    """
    return {
        "kind": "active_edit",
        "lock_id": lock.id,
        "agent": lock.agent,
        "session_id": lock.session_id,
        "pid": lock.pid,
        "host": lock.host,
        "handle": f"you ({lock.agent})",
        "intent": lock.intent,
        "expires_at": lock.expires_at.isoformat(),
        "bundle_root": lock.bundle_root,
        "self": True,
    }


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
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        if as_json:
            click.echo(json.dumps({"acquired": False, "reason": "not_in_bundle"}))
        elif not as_json:
            fatal(str(e))
        sys.exit(0 if as_json else 1)

    # Normalise every path to bundle-relative POSIX form. Absolute
    # paths get rebased; out-of-bundle paths are rejected since the
    # store is per-bundle.
    rels: list[str] = []
    for raw in paths:
        rel = _bundle_relative_path(raw, root)
        if rel is None:
            if as_json:
                click.echo(
                    json.dumps(
                        {
                            "acquired": False,
                            "reason": "path_outside_bundle",
                            "path": raw,
                        }
                    )
                )
            else:
                fatal(f"path is outside the bundle: {raw}")
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
