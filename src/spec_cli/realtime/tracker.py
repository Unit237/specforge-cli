"""
Durable cursor for ``spec watch``.

Two pieces of state, both persisted under ``<bundle>/.spec/live-cursor.json``:

* **Producer cursor** — for each session id we have observed locally,
  how many turns of it have we already broadcast. Stops the watcher
  from re-broadcasting on restart.
* **Consumer cursor** — the highest ``id`` we have ever received from
  the SSE stream. Sent as ``Last-Event-ID`` on reconnect so the server
  can replay anything missed.

The file is plain JSON written atomically (write to ``.tmp`` then
rename) so a power loss can never corrupt it. Reads are tolerant —
missing or malformed file resets cleanly. We deliberately do *not*
share this state across machines; each watcher has its own cursor and
the server's monotonic ids handle cross-machine consistency.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

_TURN_KEY_IDX_RE = re.compile(r"^(\d+):")

log = logging.getLogger(__name__)

CURSOR_FILENAME = "live-cursor.json"
CURSOR_DIRNAME = ".spec"
SCHEMA_VERSION = 1
PRODUCER_BASELINE_VERSION = 1


@dataclass
class LiveCursor:
    """Per-bundle live-watch state on disk.

    Use :py:meth:`load` to read; mutate via :py:meth:`record_broadcast`
    and :py:meth:`record_received`; persist with :py:meth:`save`. All
    methods are thread-safe — the watcher's producer and consumer run
    on different threads and both write to the cursor.
    """

    bundle_root: Path
    project_id: int | None = None
    last_received_id: int | None = None
    broadcast_turns: dict[str, int] = field(default_factory=dict)
    # Stable keys for turns we successfully POSTed — survives cursor
    # inflation and stops duplicate user rows when ``broadcast_turns``
    # overshoots the local transcript length.
    posted_turn_keys: dict[str, set[str]] = field(default_factory=dict)
    # Versioned one-time baseline for the producer.  A live watcher tails new
    # turns; it must not upload every transcript already on the machine the
    # first time it starts (or retry a legacy rejected backlog forever).
    producer_baseline_version: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── factories ─────────────────────────────────────────────────

    @classmethod
    def load(cls, bundle_root: Path, project_id: int | None = None) -> "LiveCursor":
        """Read the on-disk cursor for ``bundle_root``.

        Always returns a usable cursor — a missing file means a fresh
        start; a malformed file is logged and replaced lazily on the
        next save.

        ``project_id`` is the cloud project this bundle is bound to; we
        compare against the on-disk value and *reset* the cursor when
        they disagree (the bundle was retargeted at a different
        project, so receiving someone else's events would be wrong).
        """
        path = cls._path_for(bundle_root)
        cursor = cls(bundle_root=bundle_root, project_id=project_id)
        if not path.is_file():
            return cursor
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.info("spec-live: ignoring malformed cursor at %s: %s", path, e)
            return cursor
        if not isinstance(raw, dict):
            return cursor
        if int(raw.get("schema") or 0) != SCHEMA_VERSION:
            log.info(
                "spec-live: cursor schema mismatch at %s — discarding", path
            )
            return cursor
        on_disk_pid = raw.get("project_id")
        if (
            project_id is not None
            and on_disk_pid is not None
            and int(on_disk_pid) != project_id
        ):
            log.info(
                "spec-live: cursor project changed (%s → %s) — resetting",
                on_disk_pid,
                project_id,
            )
            return cursor
        cursor.project_id = (
            int(on_disk_pid) if on_disk_pid is not None else project_id
        )
        last_id = raw.get("last_received_id")
        if isinstance(last_id, int) and last_id >= 0:
            cursor.last_received_id = last_id
        broadcast = raw.get("broadcast_turns") or {}
        if isinstance(broadcast, dict):
            cursor.broadcast_turns = {
                str(k): int(v)
                for k, v in broadcast.items()
                if isinstance(v, int) and v >= 0
            }
        posted = raw.get("posted_turn_keys") or {}
        if isinstance(posted, dict):
            cursor.posted_turn_keys = {
                str(sid): {str(k) for k in keys if isinstance(k, str)}
                for sid, keys in posted.items()
                if isinstance(keys, list)
            }
        baseline = raw.get("producer_baseline_version")
        if isinstance(baseline, int) and baseline >= 0:
            cursor.producer_baseline_version = baseline
        return cursor

    @staticmethod
    def turn_content_fingerprint(
        *,
        text: str = "",
        summary: str | None = None,
        tool_count: int = 0,
    ) -> str:
        """Stable digest of turn body — survives coalescing index remaps."""
        payload = f"{text}\0{summary or ''}\0{tool_count}"
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @staticmethod
    def turn_post_key(
        turn_idx: int,
        role: str,
        turn_at: datetime | None,
        *,
        text: str = "",
        summary: str | None = None,
        tool_count: int = 0,
    ) -> str:
        fp = LiveCursor.turn_content_fingerprint(
            text=text, summary=summary, tool_count=tool_count
        )
        if turn_at is not None:
            ts = turn_at.astimezone().replace(microsecond=0).isoformat()
            return f"{turn_idx}:{role}:{ts}:{fp}"
        return f"{turn_idx}:{role}:{fp}"

    @staticmethod
    def turn_post_key_for(turn_idx: int, turn: Any) -> str:
        """Build a dedupe key from a :class:`~spec_cli.prompts.schema.Turn`."""
        text = getattr(turn, "text", None) or ""
        summary = getattr(turn, "summary", None)
        tool_calls = getattr(turn, "tool_calls", None) or []
        return LiveCursor.turn_post_key(
            turn_idx,
            str(getattr(turn, "role", "")),
            getattr(turn, "at", None),
            text=text,
            summary=summary,
            tool_count=len(tool_calls),
        )

    # ── reads ─────────────────────────────────────────────────────

    def turns_broadcast_for(self, session_id: str) -> int:
        """How many turns of ``session_id`` have we already POSTed?"""
        with self._lock:
            return self.broadcast_turns.get(session_id, 0)

    def has_session(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self.broadcast_turns

    def is_turn_posted(self, session_id: str, turn_idx: int, turn: Any) -> bool:
        key = self.turn_post_key_for(turn_idx, turn)
        with self._lock:
            return key in self.posted_turn_keys.get(session_id, set())

    def mark_turn_posted(self, session_id: str, turn_idx: int, turn: Any) -> None:
        key = self.turn_post_key_for(turn_idx, turn)
        with self._lock:
            self.posted_turn_keys.setdefault(session_id, set()).add(key)

    def prune_posted_keys_from_index(self, session_id: str, from_idx: int) -> None:
        """Drop dedupe keys for turn indices ``>= from_idx`` after transcript shrink."""
        if from_idx < 0:
            return
        with self._lock:
            keys = self.posted_turn_keys.get(session_id)
            if not keys:
                return
            kept = {
                k
                for k in keys
                if not (
                    (m := _TURN_KEY_IDX_RE.match(k))
                    and int(m.group(1)) >= from_idx
                )
            }
            if kept:
                self.posted_turn_keys[session_id] = kept
            else:
                self.posted_turn_keys.pop(session_id, None)

    # ── writes ────────────────────────────────────────────────────

    def record_broadcast(self, session_id: str, turn_count: int) -> None:
        """Mark turns ``[0, turn_count)`` of ``session_id`` as broadcast.

        Idempotent: counts only increase. Never moves backwards (a
        legitimate concurrent producer race) so we don't accidentally
        re-broadcast.
        """
        if turn_count <= 0:
            return
        with self._lock:
            current = self.broadcast_turns.get(session_id, 0)
            if turn_count > current:
                self.broadcast_turns[session_id] = turn_count

    def clamp_broadcast(self, session_id: str, turn_count: int) -> None:
        """Lower the broadcast cursor when the local transcript shrank.

        Unlike :meth:`record_broadcast`, this may move backwards so we
        can recover from empty-skip inflation without re-POSTing history.
        """
        if turn_count < 0:
            return
        with self._lock:
            current = self.broadcast_turns.get(session_id, 0)
            if turn_count < current:
                self.broadcast_turns[session_id] = turn_count

    def record_received(self, event_id: int) -> None:
        """Mark ``event_id`` as the most recent event we have processed.

        Only moves forward (so a late-arriving in-replay row doesn't
        kick the cursor back).
        """
        if event_id < 0:
            return
        with self._lock:
            if (
                self.last_received_id is None
                or event_id > self.last_received_id
            ):
                self.last_received_id = event_id

    def mark_producer_baseline(self) -> None:
        with self._lock:
            self.producer_baseline_version = PRODUCER_BASELINE_VERSION

    # ── persistence ───────────────────────────────────────────────

    def save(self) -> None:
        """Atomic write of the cursor file.

        Writes to a sibling ``.tmp`` and renames; on POSIX that's
        crash-safe, and on Windows ``os.replace`` is atomic too.
        Failures are logged but never raised — losing the cursor means
        re-broadcasting on next start, which is recoverable; raising
        from the watcher's hot loop is not.
        """
        path = self._path_for(self.bundle_root)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.info("spec-live: cannot mkdir %s: %s", path.parent, e)
            return
        with self._lock:
            payload = {
                "schema": SCHEMA_VERSION,
                "project_id": self.project_id,
                "last_received_id": self.last_received_id,
                "broadcast_turns": dict(self.broadcast_turns),
                "posted_turn_keys": {
                    sid: sorted(keys)
                    for sid, keys in self.posted_turn_keys.items()
                },
                "producer_baseline_version": self.producer_baseline_version,
            }
        try:
            tmp_fd, tmp_name = tempfile.mkstemp(
                prefix=f"{CURSOR_FILENAME}.",
                suffix=".tmp",
                dir=str(path.parent),
            )
        except OSError as e:
            log.info("spec-live: cursor save failed: %s", e)
            return
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"))
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp_name, path)
        except OSError as e:
            log.info("spec-live: cursor save failed: %s", e)
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    # ── helpers ───────────────────────────────────────────────────

    @staticmethod
    def _path_for(bundle_root: Path) -> Path:
        return bundle_root / CURSOR_DIRNAME / CURSOR_FILENAME


__all__ = ["LiveCursor"]
