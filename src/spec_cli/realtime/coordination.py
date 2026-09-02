"""Materialized agent-session coordination for Spec Live.

The Cloud prompt stream is the append-only source of truth. This module reduces
its user/assistant/error/assistant_closed events into two disposable local files
that coding agents can read before they plan or edit:

* ``.spec/team-coordination.json`` — machine-readable state.
* ``.spec/team-coordination.md`` — concise human/agent brief.

When the last active round closes or expires, both files are removed. Durable
history remains in Cloud and the normal ``.prompts`` capture flow.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .events import IncomingEvent, ToolCallPayload
from .repository_scope import (
    path_targets_root,
    resolve_cwd,
    resolve_touched_path,
    roots_touched_by_paths,
)

COORDINATION_JSON_FILENAME = "team-coordination.json"
COORDINATION_MD_FILENAME = "team-coordination.md"
COORDINATION_HEALTH_FILENAME = "team-coordination-health.json"
COORDINATION_SCHEMA = "spec.team-coordination/v1"
COORDINATION_HEALTH_SCHEMA = "spec.team-coordination-health/v1"
DEFAULT_ROUND_FRESHNESS_SECS = 30 * 60
MAX_CLAIMED_PATHS = 128
MAX_RECENT_OUTCOMES = 32
MAX_OBJECTIVE_CHARS = 800
MAX_PROGRESS_CHARS = 800
MAX_TOOL_NAMES = 12


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _datetime_field(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _compact_text(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    text = " ".join(value.split()).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _event_text(event: IncomingEvent, *, limit: int) -> str | None:
    return _compact_text(event.text or event.summary or event.title, limit)


def _progress_text(event: IncomingEvent) -> str | None:
    # ``title`` is the session title and is repeated on completion sentinels;
    # it is not progress. Using it here would overwrite the final assistant
    # summary precisely when the round closes.
    return _compact_text(event.text or event.summary, MAX_PROGRESS_CHARS)


def _round_key(event: IncomingEvent) -> str:
    client = (event.broadcast_client_id or "legacy").strip() or "legacy"
    return ":".join(
        (
            str(event.author_user_id),
            client,
            event.source or "agent",
            event.session_id or "session",
        )
    )


def _normalize_path(raw: Any, *, bundle_root: Path, cwd: str | None) -> str | None:
    root = bundle_root.resolve()
    cwd_path = resolve_cwd(cwd)
    candidate = resolve_touched_path(raw, cwd=cwd_path, default_root=root)
    if candidate is None:
        return None
    try:
        relative = candidate.relative_to(root).as_posix()
    except ValueError:
        return None
    return relative if relative and relative != "." else None


def _tool_paths(tool: ToolCallPayload) -> list[str]:
    """Return path-like arguments without inspecting file contents or patches."""
    out: list[str] = []
    args = tool.args if isinstance(tool.args, dict) else {}
    for key in ("path", "file_path", "notebook_path", "target_path"):
        value = args.get(key)
        if isinstance(value, str):
            out.append(value)
    for key in ("paths", "files", "edits"):
        values = args.get(key)
        if not isinstance(values, list):
            continue
        for value in values[:MAX_CLAIMED_PATHS]:
            if isinstance(value, str):
                out.append(value)
            elif isinstance(value, dict):
                for nested_key in ("path", "file_path", "notebook_path"):
                    nested = value.get(nested_key)
                    if isinstance(nested, str):
                        out.append(nested)
    return out


def _event_paths(event: IncomingEvent, bundle_root: Path) -> list[str]:
    raw = list(event.paths_touched or [])
    for tool in event.tool_calls or []:
        raw.extend(_tool_paths(tool))
    seen: set[str] = set()
    out: list[str] = []
    for value in raw:
        path = _normalize_path(value, bundle_root=bundle_root, cwd=event.cwd)
        if path is None or path in seen:
            continue
        seen.add(path)
        out.append(path)
        if len(out) >= MAX_CLAIMED_PATHS:
            break
    return out


def event_targets_bundle(event: IncomingEvent, bundle_root: Path) -> bool:
    """Return whether a repository-neutral event proves bundle ownership.

    Workspace prompt rows carry ``project_id = 0`` and are visible to every
    registered watcher.  A local coordination mirror must therefore admit
    them only when their working directory or exact path telemetry intersects
    this bundle.  Ambiguous relative paths without a working directory fail
    closed instead of being interpreted relative to every repository.
    """
    root = bundle_root.expanduser().resolve()
    cwd = resolve_cwd(event.cwd)
    if cwd is not None and path_targets_root(cwd, root):
        return True
    raw_paths = list(event.paths_touched or [])
    for tool in event.tool_calls or []:
        raw_paths.extend(_tool_paths(tool))
    return root in roots_touched_by_paths(raw_paths, cwd=cwd, roots=[root])


@dataclass
class AgentRound:
    key: str
    generation: int
    author_user_id: int
    author_handle: str | None
    author_name: str
    source: str
    session_id: str
    broadcast_client_id: str | None
    branch: str | None
    cwd: str | None
    model: str | None
    phase: str | None
    objective: str
    started_at: datetime
    updated_at: datetime
    latest_event_id: int
    last_assistant_event_id: int | None = None
    status: str = "active"
    progress: str | None = None
    outcome: str | None = None
    claimed_paths: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)

    @property
    def author_display(self) -> str:
        return f"@{self.author_handle}" if self.author_handle else self.author_name

    @classmethod
    def from_json(cls, value: Any) -> "AgentRound | None":
        if not isinstance(value, dict):
            return None
        author = value.get("author") if isinstance(value.get("author"), dict) else {}
        started_at = _datetime_field(value.get("started_at"))
        updated_at = _datetime_field(value.get("updated_at"))
        key = str(value.get("key") or "").strip()
        session_id = str(value.get("session_id") or "").strip()
        if not key or not session_id or started_at is None or updated_at is None:
            return None
        try:
            generation = max(1, int(value.get("generation") or 1))
            latest_event_id = int(value.get("latest_event_id") or -1)
            last_assistant = value.get("last_assistant_event_id")
            last_assistant_event_id = (
                int(last_assistant) if last_assistant is not None else None
            )
            author_user_id = int(author.get("user_id") or 0)
        except (TypeError, ValueError):
            return None
        return cls(
            key=key,
            generation=generation,
            author_user_id=author_user_id,
            author_handle=str(author.get("handle") or "").strip() or None,
            author_name=str(author.get("name") or "Agent")[:200],
            source=str(value.get("source") or "agent")[:80],
            session_id=session_id[:500],
            broadcast_client_id=(
                str(value.get("broadcast_client_id") or "").strip() or None
            ),
            branch=str(value.get("branch") or "").strip() or None,
            cwd=str(value.get("cwd") or "").strip() or None,
            model=str(value.get("model") or "").strip() or None,
            phase=str(value.get("phase") or "").strip() or None,
            objective=(
                _compact_text(str(value.get("objective") or ""), MAX_OBJECTIVE_CHARS)
                or "(prompt unavailable)"
            ),
            started_at=started_at,
            updated_at=updated_at,
            latest_event_id=latest_event_id,
            last_assistant_event_id=last_assistant_event_id,
            status=str(value.get("status") or "active")[:40],
            progress=_compact_text(
                str(value.get("progress") or ""), MAX_PROGRESS_CHARS
            ),
            outcome=_compact_text(
                str(value.get("outcome") or ""), MAX_PROGRESS_CHARS
            ),
            claimed_paths=[
                str(path)
                for path in value.get("claimed_paths") or []
                if isinstance(path, str)
            ][:MAX_CLAIMED_PATHS],
            tools=[
                str(name)
                for name in value.get("tools") or []
                if isinstance(name, str)
            ][:MAX_TOOL_NAMES],
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "generation": self.generation,
            "author": {
                "user_id": self.author_user_id,
                "handle": self.author_handle,
                "name": self.author_name,
            },
            "source": self.source,
            "session_id": self.session_id,
            "broadcast_client_id": self.broadcast_client_id,
            "branch": self.branch,
            "cwd": self.cwd,
            "model": self.model,
            "phase": self.phase,
            "objective": self.objective,
            "status": self.status,
            "progress": self.progress,
            "outcome": self.outcome,
            "claimed_paths": list(self.claimed_paths),
            "tools": list(self.tools),
            "started_at": self.started_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "latest_event_id": self.latest_event_id,
            "last_assistant_event_id": self.last_assistant_event_id,
        }


class CoordinationCache:
    """Thread-safe reduction of prompt events into active rounds and handoffs."""

    def __init__(
        self,
        bundle_root: Path,
        *,
        freshness_secs: float = DEFAULT_ROUND_FRESHNESS_SECS,
    ) -> None:
        self.bundle_root = bundle_root.resolve()
        self._freshness = timedelta(seconds=max(1.0, float(freshness_secs)))
        self._active: dict[str, AgentRound] = {}
        self._history: list[AgentRound] = []
        self._generation_by_key: dict[str, int] = {}
        self._last_event_id = -1
        self._changed_at: datetime | None = None
        self._lock = threading.Lock()

    def restore_snapshot(
        self,
        snapshot: dict[str, Any] | None,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Restore a fresh local projection before reconnecting to Cloud.

        The mirror is disposable, but retaining a still-fresh snapshot across
        watcher restarts prevents a quiet long-running agent from disappearing
        merely because no new SSE frame arrived during the restart window.
        """
        if not isinstance(snapshot, dict) or snapshot.get("schema") != COORDINATION_SCHEMA:
            return False
        cutoff = _utc(now) - self._freshness
        active = [
            row
            for raw in snapshot.get("active") or []
            if (row := AgentRound.from_json(raw)) is not None
            and row.status == "active"
            and row.updated_at >= cutoff
        ]
        history = [
            row
            for raw in snapshot.get("recent_outcomes") or []
            if (row := AgentRound.from_json(raw)) is not None
            and row.updated_at >= cutoff
        ]
        if not active:
            return False
        with self._lock:
            self._active = {row.key: row for row in active}
            self._history = history[-MAX_RECENT_OUTCOMES:]
            self._generation_by_key = {
                row.key: max(
                    row.generation,
                    self._generation_by_key.get(row.key, 0),
                )
                for row in [*active, *history]
            }
            ids = [row.latest_event_id for row in [*active, *history]]
            self._last_event_id = max(ids, default=-1)
            self._changed_at = _datetime_field(snapshot.get("updated_at")) or _utc(now)
        return True

    def accepts_event(self, event: IncomingEvent, *, project_id: int) -> bool:
        """Apply project rows and only repository-proven workspace rows."""
        if event.project_id == project_id:
            return True
        return event.project_id == 0 and (
            self.tracks_event_round(event)
            or event_targets_bundle(event, self.bundle_root)
        )

    def _new_round_locked(
        self,
        event: IncomingEvent,
        *,
        key: str,
        at: datetime,
        objective: str,
        assistant_started: bool = False,
    ) -> AgentRound:
        generation = self._generation_by_key.get(key, 0) + 1
        self._generation_by_key[key] = generation
        return AgentRound(
            key=key,
            generation=generation,
            author_user_id=event.author_user_id,
            author_handle=event.author_handle,
            author_name=event.author_name,
            source=event.source or "agent",
            session_id=event.session_id,
            broadcast_client_id=event.broadcast_client_id,
            branch=event.branch,
            cwd=event.cwd,
            model=event.model,
            phase=event.phase,
            objective=objective,
            started_at=_utc(event.turn_at or event.received_at),
            updated_at=at,
            latest_event_id=event.id,
            last_assistant_event_id=event.id if assistant_started else None,
            claimed_paths=_event_paths(event, self.bundle_root),
        )

    def _apply_user_locked(self, event: IncomingEvent, *, key: str, at: datetime) -> None:
        previous = self._active.pop(key, None)
        if previous is not None:
            previous.status = "completed"
            previous.outcome = previous.progress or "Superseded by a new prompt."
            previous.updated_at = at
            self._history.append(previous)
        objective = _event_text(event, limit=MAX_OBJECTIVE_CHARS) or "(prompt unavailable)"
        self._active[key] = self._new_round_locked(
            event,
            key=key,
            at=at,
            objective=objective,
        )

    def _update_round_locked(
        self,
        current: AgentRound,
        event: IncomingEvent,
        *,
        at: datetime,
    ) -> None:
        current.updated_at = at
        current.latest_event_id = event.id
        current.branch = event.branch or current.branch
        current.cwd = event.cwd or current.cwd
        current.model = event.model or current.model
        current.phase = event.phase or current.phase
        current.progress = _progress_text(event) or current.progress
        if event.role == "assistant":
            current.last_assistant_event_id = event.id
        for path in _event_paths(event, self.bundle_root):
            if (
                path not in current.claimed_paths
                and len(current.claimed_paths) < MAX_CLAIMED_PATHS
            ):
                current.claimed_paths.append(path)
        for tool in event.tool_calls or []:
            if tool.name not in current.tools and len(current.tools) < MAX_TOOL_NAMES:
                current.tools.append(tool.name)

    def apply_event(self, event: IncomingEvent) -> bool:
        """Apply one monotonic Cloud event; return whether board state changed."""
        if event.role == "presence" or event.id < 0:
            return False
        with self._lock:
            if event.id <= self._last_event_id:
                return False
            self._last_event_id = event.id
            key = _round_key(event)
            at = _utc(event.received_at or event.turn_at)

            if event.role == "user":
                self._apply_user_locked(event, key=key, at=at)
                self._changed_at = at
                self._prune_history_locked()
                return True

            current = self._active.get(key)
            if current is None and event.role == "assistant":
                current = self._new_round_locked(
                    event,
                    key=key,
                    at=at,
                    objective=(
                        _compact_text(event.title, MAX_OBJECTIVE_CHARS)
                        or "(earlier prompt outside replay window)"
                    ),
                    assistant_started=True,
                )
                self._active[key] = current

            if current is None:
                return False

            # A delayed close sentinel references the assistant row it closes.
            # Never let a close from the prior generation terminate a newer
            # user prompt in the same long-lived agent session.
            if (
                event.role == "assistant_closed"
                and event.closes_event_id is not None
                and event.closes_event_id != current.last_assistant_event_id
            ):
                return False

            self._update_round_locked(current, event, at=at)

            # Codex emits ``assistant_closed`` after each stable assistant
            # bubble, including progress commentary between tool calls. That
            # is a transport boundary, not a task boundary. Keep the round and
            # its claimed paths active until a final-answer bubble closes.
            if event.role == "assistant_closed" and current.phase == "commentary":
                self._changed_at = at
                self._prune_history_locked()
                return True

            if event.role in {"assistant_closed", "error"}:
                current.status = "failed" if event.role == "error" else "completed"
                current.outcome = current.progress or (
                    "Agent reported an error." if event.role == "error" else "Round completed."
                )
                self._active.pop(key, None)
                self._history.append(current)
            self._changed_at = at
            self._prune_history_locked()
            return True

    def tracks_event_round(self, event: IncomingEvent) -> bool:
        """Whether an event belongs to an already-admitted active round."""
        with self._lock:
            return _round_key(event) in self._active

    def expire_stale(self, *, now: datetime | None = None) -> bool:
        cutoff = _utc(now) - self._freshness
        with self._lock:
            stale = [key for key, value in self._active.items() if value.updated_at < cutoff]
            for key in stale:
                self._active.pop(key, None)
            if stale:
                self._changed_at = _utc(now)
                self._prune_history_locked()
            return bool(stale)

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        self.expire_stale(now=now)
        with self._lock:
            if not self._active:
                return None
            active = sorted(
                self._active.values(),
                key=lambda row: (row.started_at, row.key, row.generation),
            )
            cohort_start = min(row.started_at for row in active)
            recent = [row for row in self._history if row.updated_at >= cohort_start]
            recent = sorted(recent, key=lambda row: (row.updated_at, row.key))[
                -MAX_RECENT_OUTCOMES:
            ]
            files_index: dict[str, list[dict[str, str]]] = {}
            for row in active:
                for path in row.claimed_paths:
                    files_index.setdefault(path, []).append(
                        {
                            "key": row.key,
                            "kind": "task_claim",
                            "agent": row.source,
                            "author": row.author_display,
                            "author_user_id": row.author_user_id,
                            "session_id": row.session_id,
                            "broadcast_client_id": row.broadcast_client_id,
                            "objective": row.objective,
                        }
                    )
            return {
                "schema": COORDINATION_SCHEMA,
                "updated_at": (self._changed_at or _utc(now)).isoformat(),
                "active": [row.to_json() for row in active],
                "recent_outcomes": [row.to_json() for row in recent],
                "files_index": dict(sorted(files_index.items())),
            }

    def _prune_history_locked(self) -> None:
        if not self._active:
            self._history.clear()
            return
        cohort_start = min(row.started_at for row in self._active.values())
        self._history = [row for row in self._history if row.updated_at >= cohort_start][
            -MAX_RECENT_OUTCOMES:
        ]


def render_coordination_markdown(snapshot: dict[str, Any]) -> str:
    lines = [
        "# Spec Live — agent coordination (auto-generated)",
        "",
        "Read this before planning or editing. Avoid duplicating active work;",
        "for overlapping paths run `spec locks check <bundle-relative-path>`.",
        "This brief is advisory, not a distributed mutex. It disappears when",
        "the last active agent round finishes or expires.",
        "",
        f"**Updated:** {snapshot.get('updated_at', '?')} (UTC)",
        "",
    ]
    files_index = snapshot.get("files_index") or {}
    if files_index:
        lines.extend(["## Claimed path index", ""])
        for path, holders in files_index.items():
            labels = [f"{row.get('author')} ({row.get('agent')})" for row in holders]
            lines.append(f"- `{path}` → {', '.join(labels)}")
        lines.append("")

    lines.extend(["## Active agent rounds", ""])
    for row in snapshot.get("active") or []:
        author = row.get("author") or {}
        display = (
            f"@{author.get('handle')}"
            if author.get("handle")
            else author.get("name") or "(unknown)"
        )
        source = row.get("source") or "agent"
        branch = row.get("branch") or "?"
        lines.append(f"### {display} · {source} · `{branch}`")
        lines.append(f"- **Objective:** {row.get('objective') or '(unknown)'}")
        if row.get("progress"):
            lines.append(f"- **Latest:** {row['progress']}")
        if row.get("claimed_paths"):
            paths = ", ".join(f"`{path}`" for path in row["claimed_paths"])
            lines.append(f"- **Claimed/touched:** {paths}")
        if row.get("tools"):
            lines.append(f"- **Tools:** {', '.join(row['tools'])}")
        lines.append(
            f"- **Session:** `{row.get('session_id') or '?'}` · updated {row.get('updated_at') or '?'}"
        )
        lines.append("")

    recent = snapshot.get("recent_outcomes") or []
    if recent:
        lines.extend(["## Recent handoffs from this active cohort", ""])
        for row in recent:
            author = row.get("author") or {}
            display = (
                f"@{author.get('handle')}"
                if author.get("handle")
                else author.get("name") or "(unknown)"
            )
            lines.append(
                f"- **{display} · {row.get('source') or 'agent'} · {row.get('status') or 'completed'}:** "
                f"{row.get('outcome') or row.get('objective') or '(no outcome)'}"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _coordination_health(*, updated_at: str, active_count: int) -> dict[str, Any]:
    return {
        "schema": COORDINATION_HEALTH_SCHEMA,
        "updated_at": updated_at,
        "active_count": max(0, int(active_count)),
    }


def _json_text(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


class TeamCoordinationMirror:
    def __init__(self, bundle_root: Path) -> None:
        self.spec_dir = bundle_root / ".spec"
        self.json_path = self.spec_dir / COORDINATION_JSON_FILENAME
        self.md_path = self.spec_dir / COORDINATION_MD_FILENAME
        self.health_path = self.spec_dir / COORDINATION_HEALTH_FILENAME

    def sync(self, cache: CoordinationCache, *, now: datetime | None = None) -> bool:
        snapshot = cache.snapshot(now=now)
        if snapshot is None:
            health_changed = self._write_if_changed(
                self.health_path,
                _json_text(
                    _coordination_health(
                        updated_at=_utc(now).isoformat(),
                        active_count=0,
                    )
                ),
            )
            changed = health_changed
            for path in (self.json_path, self.md_path):
                try:
                    path.unlink()
                    changed = True
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            return changed
        json_text = _json_text(snapshot)
        md_text = render_coordination_markdown(snapshot)
        health_text = _json_text(
            _coordination_health(
                updated_at=str(snapshot.get("updated_at") or ""),
                active_count=len(snapshot.get("active") or []),
            )
        )
        json_changed = self._write_if_changed(self.json_path, json_text)
        md_changed = self._write_if_changed(self.md_path, md_text)
        health_changed = self._write_if_changed(self.health_path, health_text)
        return json_changed or md_changed or health_changed

    def _write_if_changed(self, target: Path, text: str) -> bool:
        try:
            if target.is_file() and target.read_text(encoding="utf-8") == text:
                return False
            self.spec_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f"{target.name}.", suffix=".tmp", dir=str(self.spec_dir)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
                    handle.flush()
                    try:
                        os.fsync(handle.fileno())
                    except OSError:
                        pass
                os.replace(tmp_name, target)
            except OSError:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            return True
        except OSError:
            return False


def _read_projection(
    bundle_root: Path,
    *,
    filename: str,
    schema: str,
) -> dict[str, Any] | None:
    path = bundle_root.resolve() / ".spec" / filename
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict) or body.get("schema") != schema:
        return None
    return body


def read_team_coordination(bundle_root: Path) -> dict[str, Any] | None:
    """Read the generated task-claim projection, failing safely on absence.

    The file is intentionally ephemeral, so ``None`` means either no active
    rounds or unavailable coordination data. Callers combine it with the
    always-present coordination health projection before deciding whether a
    path is truly clear.
    """
    return _read_projection(
        bundle_root,
        filename=COORDINATION_JSON_FILENAME,
        schema=COORDINATION_SCHEMA,
    )


def read_team_coordination_health(bundle_root: Path) -> dict[str, Any] | None:
    return _read_projection(
        bundle_root,
        filename=COORDINATION_HEALTH_FILENAME,
        schema=COORDINATION_HEALTH_SCHEMA,
    )


__all__ = [
    "COORDINATION_JSON_FILENAME",
    "COORDINATION_MD_FILENAME",
    "COORDINATION_HEALTH_FILENAME",
    "COORDINATION_HEALTH_SCHEMA",
    "COORDINATION_SCHEMA",
    "CoordinationCache",
    "TeamCoordinationMirror",
    "event_targets_bundle",
    "read_team_coordination",
    "read_team_coordination_health",
    "render_coordination_markdown",
]
