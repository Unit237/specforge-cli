"""One conflict decision for every Spec pre-edit consumer.

The inputs remain intentionally distinct:

* task claims come from the Cloud-backed coordination projection;
* dirty-tree presence describes cross-machine repository state;
* active edits are short local tool-call leases.

This module owns how those signals become ``clear``, ``conflict``, or
``unknown`` so the CLI and deterministic hooks cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..config import BundleNotFoundError, find_bundle_root
from ..git import repo_toplevel
from .active_edits import ActiveEditLock, ActiveEditsStore, paths_overlap
from .coordination import DEFAULT_ROUND_FRESHNESS_SECS, read_team_coordination
from .presence_mirror import read_team_presence
from .team_editing_brief import (
    DEFAULT_LOCKS_MIRROR_STALE_SECS,
    _compute_pull_alerts,
    team_presence_mirror_stale,
)


ConflictState = Literal["clear", "conflict", "unknown"]


@dataclass(frozen=True)
class ConflictAssessment:
    state: ConflictState
    path: str
    holders: list[dict]
    pull_alerts: list[dict]
    reason: str | None = None

    @property
    def clear(self) -> bool:
        return self.state == "clear"

    def to_json(self) -> dict:
        body = {
            "state": self.state,
            "clear": self.clear,
            "path": self.path,
            "holders": self.holders,
            "pull_alerts": self.pull_alerts,
        }
        if self.reason:
            body["reason"] = self.reason
        return body


def resolve_coordination_path(
    raw_path: str,
    *,
    cwd: Path | None = None,
) -> tuple[Path | None, str | None]:
    """Resolve PATH against a bundle, generated mirror, or Git worktree.

    Read-only lock checks should not report ``not_in_bundle`` merely because a
    repository uses Spec's generated mirrors without a manifest. A Git root is
    still returned as the local lease namespace; absent mirrors then produce
    the explicit ``unknown`` state.
    """
    here = (cwd or Path.cwd()).resolve()
    raw = Path(raw_path).expanduser()

    if not raw.is_absolute():
        try:
            bundle_root = find_bundle_root(here)
        except BundleNotFoundError:
            bundle_root = None
        if bundle_root is not None:
            candidate = (bundle_root / raw).resolve()
            rel = _relative(candidate, bundle_root)
            if rel is not None:
                return bundle_root.resolve(), rel
        candidate = (here / raw).resolve()
    else:
        candidate = raw.resolve()

    start = candidate if candidate.is_dir() else candidate.parent
    root = _nearest_coordination_root(start)
    if root is None:
        root = repo_toplevel(start)
    if root is None:
        return None, None
    return root.resolve(), _relative(candidate, root)


def assess_path_conflict(
    bundle_root: Path,
    rel_path: str,
    *,
    include_self_dirty: bool = False,
    include_active_edits: bool = True,
    caller_agent: str | None = None,
    caller_session_id: str | None = None,
    max_presence_age_secs: float = DEFAULT_LOCKS_MIRROR_STALE_SECS,
) -> ConflictAssessment:
    """Reduce every current coordination signal into one stable decision."""
    active = (
        _active_holders(
            bundle_root,
            rel_path,
            caller_agent=caller_agent,
            caller_session_id=caller_session_id,
        )
        if include_active_edits
        else []
    )

    presence = read_team_presence(bundle_root)
    presence_fresh = not team_presence_mirror_stale(
        presence,
        max_age_secs=max_presence_age_secs,
    )
    dirty = (
        _dirty_holders(
            presence or {},
            rel_path,
            include_self=include_self_dirty,
        )
        if presence_fresh
        else []
    )
    pull_alerts = _compute_pull_alerts(presence or {}) if presence_fresh else []

    coordination = read_team_coordination(bundle_root)
    coordination_fresh = not team_presence_mirror_stale(
        coordination,
        max_age_secs=float(DEFAULT_ROUND_FRESHNESS_SECS),
    )
    claims = (
        _task_claim_holders(
            coordination or {},
            rel_path,
            caller_agent=caller_agent,
            caller_session_id=caller_session_id,
        )
        if coordination_fresh
        else []
    )

    holders = _deduplicate_holders(active + claims + dirty)
    if holders:
        return ConflictAssessment(
            state="conflict",
            path=rel_path,
            holders=holders,
            pull_alerts=pull_alerts,
        )
    if not presence_fresh:
        return ConflictAssessment(
            state="unknown",
            path=rel_path,
            holders=[],
            pull_alerts=[],
            reason="no_live_data" if presence is None else "stale_mirror",
        )
    return ConflictAssessment(
        state="clear",
        path=rel_path,
        holders=[],
        pull_alerts=pull_alerts,
    )


def _nearest_coordination_root(start: Path) -> Path | None:
    current = start.resolve()
    for _ in range(128):
        spec_dir = current / ".spec"
        if (
            (current / "spec.yaml").is_file()
            or (spec_dir / "team-presence.json").is_file()
            or (spec_dir / "team-coordination.json").is_file()
        ):
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None


def _relative(path: Path, root: Path) -> str | None:
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None
    return rel if rel and rel != "." else None


def _entries_for_path(body: dict, rel_path: str) -> list[dict]:
    index = body.get("files_index")
    if not isinstance(index, dict):
        return []
    result: list[dict] = []
    for scope, entries in index.items():
        if not isinstance(scope, str) or not paths_overlap(scope, rel_path):
            continue
        if not isinstance(entries, list):
            continue
        result.extend(dict(entry) for entry in entries if isinstance(entry, dict))
    return result


def _dirty_holders(body: dict, rel_path: str, *, include_self: bool) -> list[dict]:
    holders: list[dict] = []
    for entry in _entries_for_path(body, rel_path):
        if not include_self and bool(entry.get("self")):
            continue
        entry.setdefault("kind", "dirty_tree")
        holders.append(entry)
    return holders


def _task_claim_holders(
    body: dict,
    rel_path: str,
    *,
    caller_agent: str | None,
    caller_session_id: str | None,
) -> list[dict]:
    holders: list[dict] = []
    for entry in _entries_for_path(body, rel_path):
        if _same_caller(
            entry.get("agent"),
            entry.get("session_id"),
            caller_agent,
            caller_session_id,
        ):
            continue
        entry.setdefault("kind", "task_claim")
        entry.setdefault("handle", entry.get("author"))
        holders.append(entry)
    return holders


def _active_holders(
    bundle_root: Path,
    rel_path: str,
    *,
    caller_agent: str | None,
    caller_session_id: str | None,
) -> list[dict]:
    try:
        locks = ActiveEditsStore(bundle_root).holders_for(rel_path)
    except Exception:  # noqa: BLE001
        return []
    return [
        _active_holder(lock)
        for lock in locks
        if not _same_caller(
            lock.agent,
            lock.session_id,
            caller_agent,
            caller_session_id,
        )
    ]


def _active_holder(lock: ActiveEditLock) -> dict:
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


def _same_caller(
    holder_agent: object,
    holder_session: object,
    caller_agent: str | None,
    caller_session: str | None,
) -> bool:
    if not caller_agent or caller_session is None:
        return False
    return (
        str(holder_agent or "").strip().casefold() == caller_agent.strip().casefold()
        and str(holder_session or "") == caller_session
    )


def _deduplicate_holders(holders: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, str, str]] = set()
    result: list[dict] = []
    for holder in holders:
        key = (
            str(holder.get("kind") or ""),
            str(holder.get("lock_id") or holder.get("key") or ""),
            str(holder.get("agent") or holder.get("handle") or ""),
            str(holder.get("session_id") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(holder)
    return result


__all__ = [
    "ConflictAssessment",
    "ConflictState",
    "assess_path_conflict",
    "resolve_coordination_path",
]
