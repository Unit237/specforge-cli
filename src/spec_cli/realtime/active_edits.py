"""
Local active-edit locks for single-user multi-agent coordination.

``team-presence.json`` answers "is one of my **teammates** editing file
X right now?" — it's a network coordination layer for distinct users on
distinct machines. The file you're looking at answers a different
question: **"is one of MY OWN AI agents editing file X right now?"**

A developer commonly has two or three agents running side-by-side
inside the same bundle:

* Claude Code in a terminal, halfway through a multi-file refactor.
* Cursor's Agent pane (or regular chat) editing the same files.
* Codex Desktop or a background agent doing a long-running review.

Each of them happily writes into the same working tree. ``git`` sees
"all of these edits are mine"; ``team-presence.json`` says "no
teammate is dirty here" (correctly — it's all you). Neither catches
the case where Cursor's agent is rewriting ``auth.py`` while Claude
Code is *also* applying an Edit to the same line range. The user
notices only after the second agent stomps on the first's diff.

This module owns a small per-bundle file, ``.spec/active-edits.json``,
that records short-lived **lock entries** keyed by ``(agent,
session_id, paths)``. Each entry has a TTL (default 5 minutes) so a
crashed agent never holds a lock forever. Acquire / release flow:

* Before a write tool call, the agent's pre-tool hook calls
  :py:meth:`ActiveEditsStore.acquire` with the file paths it's about
  to modify. The store returns a lock id; the hook surfaces a warning
  if another agent already holds an overlapping lock.
* After the write tool call, the post-tool hook calls
  :py:meth:`ActiveEditsStore.release` with the lock id.
* ``spec locks check <path>`` merges these locks with the team-
  presence holders so an agent / human sees both layers at once.

The file is **never** broadcast to teammates over the network. It is
strictly a local-machine coordination mechanism. The on-disk schema is
versioned (``schema: 1``) so future shape changes are detectable.

Cross-process correctness:

* Reads + writes go through ``fcntl.flock`` on
  ``.spec/active-edits.lock`` so two agents writing at the same
  instant serialise.
* The data file is replaced atomically (write-temp + rename), matching
  ``team-presence.json`` and ``LiveCursor``.
* Missing / malformed files are treated as "no locks", logged, and
  replaced lazily on the next save. We deliberately fail open: a
  broken lock file should never prevent edits.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import socket
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

log = logging.getLogger(__name__)

ACTIVE_EDITS_DIR = ".spec"
ACTIVE_EDITS_FILENAME = "active-edits.json"
ACTIVE_EDITS_LOCKFILE = "active-edits.lock"
ACTIVE_EDITS_SCHEMA_VERSION = 1

# Default lock duration. Long enough to span a multi-edit tool chain
# (Claude / Cursor can spend a minute on a big refactor), short enough
# that a crashed process doesn't deadlock teammates for hours. Override
# per-acquire via ``ttl_secs``.
DEFAULT_LOCK_TTL_SECS = 300

# Hard cap so a buggy caller can't pin a lock for a week. 1 hour.
MAX_LOCK_TTL_SECS = 3600

# Canonical agent identifiers. Free-form strings are accepted by the
# store; this set is exposed so callers can use stable values that the
# brief / check renderers know how to colour-code.
KNOWN_AGENTS = frozenset(
    {"claude_code", "cursor", "codex", "compress", "manual", "spec_cli"}
)


@dataclass
class ActiveEditLock:
    """One lock entry, as persisted on disk.

    Field meanings:

    * ``id`` — uuid4 string; the handle a caller uses for
      :py:meth:`ActiveEditsStore.release`. Generated on acquire.
    * ``paths`` — bundle-relative paths this lock covers. Stored as
      a list because tools like ``MultiEdit`` write to several files
      in one atomic operation; one lock can guard the whole batch.
    * ``agent`` — short, stable identifier for the AI agent. See
      :data:`KNOWN_AGENTS`.
    * ``session_id`` — the agent's session/conversation id when
      available (Claude Code session id, Cursor composer id, Codex
      thread id). Used to deduplicate "the same agent re-acquires"
      and to render rich attributions ("@you · claude_code · session
      abc123").
    * ``pid`` / ``host`` — process id and machine name. Lets a hook
      detect "the agent that took this lock died" by checking
      whether ``pid`` is still running on this host.
    * ``started_at`` / ``expires_at`` — UTC timestamps. ``expires_at
      <= now`` means the lock is considered released even if it
      hasn't been physically pruned yet.
    * ``intent`` — short human-readable hint about what the lock is
      for (typically the tool name: ``"Edit"`` / ``"Write"`` /
      ``"MultiEdit"``). Helps a teammate / reviewer disambiguate
      between "agent is making a quick edit" and "agent is doing a
      multi-file refactor".
    * ``note`` — optional free-form text. Reserved for richer
      attribution surfaces (e.g. "fixing the cursor adapter
      placeholder bug").
    """

    id: str
    paths: list[str]
    agent: str
    session_id: str | None
    pid: int
    host: str
    started_at: datetime
    expires_at: datetime
    intent: str | None = None
    note: str | None = None

    # ── lifecycle helpers ─────────────────────────────────────────

    def is_expired(self, *, now: datetime | None = None) -> bool:
        """True when ``expires_at`` is in the past.

        ``now`` is injectable for testing. Real callers pass nothing
        and we use the current UTC time.
        """
        ref = now or datetime.now(timezone.utc)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp <= ref

    def to_json(self) -> dict[str, Any]:
        """Serialise to the on-disk shape. Always uses ISO-8601 UTC."""
        return {
            "id": self.id,
            "paths": list(self.paths),
            "agent": self.agent,
            "session_id": self.session_id,
            "pid": int(self.pid),
            "host": self.host,
            "started_at": _iso(self.started_at),
            "expires_at": _iso(self.expires_at),
            "intent": self.intent,
            "note": self.note,
        }

    @classmethod
    def from_json(cls, raw: Any) -> "ActiveEditLock | None":
        """Tolerant parse: any missing required field returns None so a
        partially-corrupt file degrades to "skip this row" rather than
        crashing every consumer."""
        if not isinstance(raw, dict):
            return None
        try:
            lid = str(raw["id"])
            paths = raw.get("paths") or []
            if not isinstance(paths, list):
                return None
            paths = [str(p) for p in paths if isinstance(p, str) and p]
            if not paths:
                return None
            agent = str(raw.get("agent") or "manual")
            session_id = raw.get("session_id")
            session_id = str(session_id) if isinstance(session_id, str) else None
            pid = int(raw.get("pid") or 0)
            host = str(raw.get("host") or "")
            started_at = _parse_iso_utc(raw.get("started_at"))
            expires_at = _parse_iso_utc(raw.get("expires_at"))
            if started_at is None or expires_at is None:
                return None
            intent = raw.get("intent")
            intent = str(intent) if isinstance(intent, str) and intent else None
            note = raw.get("note")
            note = str(note) if isinstance(note, str) and note else None
        except (KeyError, TypeError, ValueError):
            return None
        return cls(
            id=lid,
            paths=paths,
            agent=agent,
            session_id=session_id,
            pid=pid,
            host=host,
            started_at=started_at,
            expires_at=expires_at,
            intent=intent,
            note=note,
        )


@dataclass
class ActiveEditConflict:
    """One lock blocking the path you're trying to acquire.

    Returned by :py:meth:`ActiveEditsStore.acquire`. Each conflict
    enumerates the overlap precisely so a renderer can say "Cursor is
    editing ``auth.py`` (and 2 other files) — started 12 s ago,
    expires in 4 m 48 s".

    Same-agent same-session conflicts are *not* returned: a single
    agent re-acquiring is treated as a renewal, not a clash. Only
    cross-agent or cross-session overlaps surface.
    """

    lock: ActiveEditLock
    overlapping_paths: list[str]


class ActiveEditsStore:
    """Read / mutate ``.spec/active-edits.json`` with cross-process safety.

    One instance per ``(bundle_root)``. Methods are thread-safe within
    the process and serialised across processes via an OS-level
    ``flock`` on the sibling ``.spec/active-edits.lock``.
    """

    def __init__(self, bundle_root: Path) -> None:
        self._bundle_root = bundle_root.resolve()
        self._spec_dir = self._bundle_root / ACTIVE_EDITS_DIR
        self._path = self._spec_dir / ACTIVE_EDITS_FILENAME
        self._lockfile = self._spec_dir / ACTIVE_EDITS_LOCKFILE
        # Intra-process serialisation: every method takes this before
        # entering the flock'd region, so two threads in the same
        # process don't both wait on flock with stale state.
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    # ── reads ─────────────────────────────────────────────────────

    def list(
        self,
        *,
        include_expired: bool = False,
        now: datetime | None = None,
    ) -> list[ActiveEditLock]:
        """All locks currently in the file.

        Expired locks are filtered out unless ``include_expired`` is
        True (``spec locks list --include-expired`` for debugging
        / ``prune`` previewing).
        """
        body = self._read()
        ref = now or datetime.now(timezone.utc)
        out: list[ActiveEditLock] = []
        for raw in body.get("locks") or []:
            lock = ActiveEditLock.from_json(raw)
            if lock is None:
                continue
            if not include_expired and lock.is_expired(now=ref):
                continue
            out.append(lock)
        return out

    def holders_for(
        self,
        path: str,
        *,
        now: datetime | None = None,
    ) -> list[ActiveEditLock]:
        """Every non-expired lock that covers ``path`` (bundle-relative)."""
        rel = _normalize_path(path)
        ref = now or datetime.now(timezone.utc)
        return [
            lock
            for lock in self.list(now=ref)
            if rel in lock.paths
        ]

    # ── writes ────────────────────────────────────────────────────

    def acquire(
        self,
        paths: Iterable[str],
        *,
        agent: str,
        session_id: str | None = None,
        ttl_secs: float = DEFAULT_LOCK_TTL_SECS,
        intent: str | None = None,
        note: str | None = None,
        now: datetime | None = None,
    ) -> tuple[ActiveEditLock, list[ActiveEditConflict]]:
        """Try to grab a lock on ``paths``.

        Returns ``(lock, conflicts)``:

        * ``lock`` is always created — acquire is **advisory**, not
          mandatory. Returning a lock even when there are conflicts
          lets the caller decide what to do (warn the user, block
          the tool call, or push through). This matches the
          ``spec locks check`` contract.
        * ``conflicts`` is the list of *other-agent* / *other-session*
          locks that overlap the requested paths. Empty when the
          acquire is clean.

        Same agent + session is treated as a **renewal**: any existing
        lock with that ``(agent, session_id)`` is replaced (its id
        becomes invalid) and the new lock subsumes the paths. This
        lets a long-running agent loop call ``acquire`` on every tool
        call without piling up overlapping locks.

        ``ttl_secs`` is clamped to :data:`MAX_LOCK_TTL_SECS`.
        """
        rel_paths = [_normalize_path(p) for p in paths if p]
        rel_paths = [p for p in rel_paths if p]
        if not rel_paths:
            raise ValueError("acquire() requires at least one non-empty path")
        ttl = max(1.0, min(float(ttl_secs), float(MAX_LOCK_TTL_SECS)))
        ref = now or datetime.now(timezone.utc)
        expires = ref + timedelta(seconds=ttl)

        with self._lock, _flock(self._lockfile):
            body = self._read_locked()
            existing: list[ActiveEditLock] = []
            for raw in body.get("locks") or []:
                lock = ActiveEditLock.from_json(raw)
                if lock is None:
                    continue
                if not lock.is_expired(now=ref):
                    existing.append(lock)

            # Renewal: drop any prior lock from the same agent/session
            # before computing conflicts. Otherwise an agent looping
            # over its own work would be told "you conflict with
            # yourself".
            existing = [
                lock
                for lock in existing
                if not _is_same_caller(lock, agent=agent, session_id=session_id)
            ]

            conflicts: list[ActiveEditConflict] = []
            requested = set(rel_paths)
            for lock in existing:
                overlap = sorted(set(lock.paths) & requested)
                if overlap:
                    conflicts.append(
                        ActiveEditConflict(lock=lock, overlapping_paths=overlap)
                    )

            new_lock = ActiveEditLock(
                id=str(uuid.uuid4()),
                paths=rel_paths,
                agent=agent.strip() or "manual",
                session_id=session_id,
                pid=os.getpid(),
                host=_host_id(),
                started_at=ref,
                expires_at=expires,
                intent=intent,
                note=note,
            )
            existing.append(new_lock)

            self._write_locked(existing, now=ref)
            return (new_lock, conflicts)

    def release(self, lock_id: str) -> bool:
        """Drop the lock with id ``lock_id``.

        Returns True when a matching entry was found and removed,
        False when nothing matched (already expired / wrong id). The
        store fails silently on unknown ids: a hook may have already
        seen the lock expire and released on its own.
        """
        if not lock_id:
            return False
        with self._lock, _flock(self._lockfile):
            body = self._read_locked()
            kept: list[ActiveEditLock] = []
            removed = False
            for raw in body.get("locks") or []:
                lock = ActiveEditLock.from_json(raw)
                if lock is None:
                    continue
                if lock.id == lock_id:
                    removed = True
                    continue
                kept.append(lock)
            if removed:
                self._write_locked(kept, now=datetime.now(timezone.utc))
            return removed

    def release_for_session(
        self, *, agent: str, session_id: str | None
    ) -> int:
        """Drop every lock matching ``(agent, session_id)``.

        Useful as a "cleanup all locks for this hook session" call
        when an agent process exits — the post-tool hook would
        normally release each lock by id, but a one-shot session
        teardown shortcut lets us reclaim on any exit path. Returns
        the count of locks removed.
        """
        with self._lock, _flock(self._lockfile):
            body = self._read_locked()
            kept: list[ActiveEditLock] = []
            removed = 0
            for raw in body.get("locks") or []:
                lock = ActiveEditLock.from_json(raw)
                if lock is None:
                    continue
                if _is_same_caller(lock, agent=agent, session_id=session_id):
                    removed += 1
                    continue
                kept.append(lock)
            if removed:
                self._write_locked(kept, now=datetime.now(timezone.utc))
            return removed

    def prune(self, *, now: datetime | None = None) -> int:
        """Physically remove expired locks. Returns count removed.

        ``list()`` already filters expired locks out of every read,
        so calling ``prune`` is purely a housekeeping step (keeps
        the JSON file small + lets external tools inspect a fresh
        view). The watcher could call this on a timer; for now it
        is invoked explicitly via ``spec locks prune``.
        """
        ref = now or datetime.now(timezone.utc)
        with self._lock, _flock(self._lockfile):
            body = self._read_locked()
            kept: list[ActiveEditLock] = []
            removed = 0
            for raw in body.get("locks") or []:
                lock = ActiveEditLock.from_json(raw)
                if lock is None:
                    removed += 1
                    continue
                if lock.is_expired(now=ref):
                    removed += 1
                    continue
                kept.append(lock)
            if removed:
                self._write_locked(kept, now=ref)
            return removed

    # ── internals ─────────────────────────────────────────────────

    def _read(self) -> dict:
        if not self._path.is_file():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError) as e:
            log.info(
                "spec-live: ignoring malformed active-edits at %s: %s",
                self._path,
                e,
            )
            return {}

    def _read_locked(self) -> dict:
        # Called from inside the flock; same as _read but the caller
        # already owns the cross-process lock.
        return self._read()

    def _write_locked(
        self,
        locks: list[ActiveEditLock],
        *,
        now: datetime,
    ) -> None:
        try:
            self._spec_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.info("spec-live: cannot mkdir %s: %s", self._spec_dir, e)
            return
        body = {
            "schema": ACTIVE_EDITS_SCHEMA_VERSION,
            "updated_at": _iso(now),
            "locks": [lock.to_json() for lock in locks],
        }
        encoded = json.dumps(body, indent=2, sort_keys=True)
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=f"{ACTIVE_EDITS_FILENAME}.",
            suffix=".tmp",
            dir=str(self._spec_dir),
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(encoded)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp_name, self._path)
        except OSError as e:
            log.info("spec-live: active-edits save failed: %s", e)
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


# ── module helpers ─────────────────────────────────────────────────


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso_utc(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except ValueError:
        return None


def _normalize_path(raw: str) -> str:
    """Make path comparisons robust to slash / dot weirdness.

    Callers may pass absolute paths, bundle-relative paths, or paths
    that have a redundant ``./`` prefix from shell expansion. We
    normalise to a POSIX-style relative path so two callers asking
    about the same file always match.
    """
    if not isinstance(raw, str):
        return ""
    s = raw.strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s


def _is_same_caller(
    lock: ActiveEditLock,
    *,
    agent: str,
    session_id: str | None,
) -> bool:
    """True when ``lock`` was taken by the same agent+session pair.

    Used for renewals: an agent calling ``acquire`` twice in a row
    should renew its own lock rather than conflict with itself.
    Session match is exact (None matches None, "abc" matches "abc");
    agent name is case-insensitive trimmed.
    """
    if (lock.agent or "").strip().lower() != (agent or "").strip().lower():
        return False
    if (lock.session_id or "") != (session_id or ""):
        return False
    return True


def _host_id() -> str:
    """Stable machine identifier. Tries ``platform.node()`` then
    ``socket.gethostname()``. Returns ``"unknown-host"`` on failure
    rather than raising — the field is informational, not contractual.
    """
    try:
        h = platform.node().strip()
        if h:
            return h
    except Exception:  # noqa: BLE001
        pass
    try:
        h = socket.gethostname().strip()
        if h:
            return h
    except Exception:  # noqa: BLE001
        pass
    return "unknown-host"


@contextmanager
def _flock(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive ``fcntl.flock`` on ``lock_path`` for the
    duration of the ``with`` block.

    POSIX-only by intent — Windows isn't a target platform for Spec
    yet, and ``fcntl`` isn't importable there. On any platform where
    ``fcntl`` is missing we fall back to a best-effort lock-file
    create-and-busy-wait loop with a short timeout, which is enough
    serialisation for the typical "one hook per second" cadence
    without pretending to be hard.
    """
    try:
        import fcntl  # type: ignore[import-not-found]
    except ImportError:
        _busy_lock(lock_path)
        try:
            yield
        finally:
            try:
                lock_path.unlink()
            except OSError:
                pass
        return

    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # No bundle dir — caller will fail on the actual write too.
        yield
        return

    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _busy_lock(lock_path: Path, *, timeout_secs: float = 2.0) -> None:
    """Windows / no-fcntl fallback: spin until we can ``O_CREAT |
    O_EXCL`` the lockfile or timeout. Bounded so a stuck holder
    doesn't deadlock the caller — we'd rather race than block."""
    deadline = time.monotonic() + max(0.1, timeout_secs)
    while time.monotonic() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
            os.close(fd)
            return
        except FileExistsError:
            time.sleep(0.05)
        except OSError:
            return


__all__ = [
    "ACTIVE_EDITS_DIR",
    "ACTIVE_EDITS_FILENAME",
    "ACTIVE_EDITS_LOCKFILE",
    "ACTIVE_EDITS_SCHEMA_VERSION",
    "DEFAULT_LOCK_TTL_SECS",
    "KNOWN_AGENTS",
    "MAX_LOCK_TTL_SECS",
    "ActiveEditConflict",
    "ActiveEditLock",
    "ActiveEditsStore",
]
