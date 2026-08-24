"""
Local edit-presence — "what files am I currently editing in this bundle?"

What we ship:

* :func:`compute_local_presence` runs ``git diff --numstat HEAD`` plus
  ``git ls-files --others --exclude-standard``, scoped to the bundle
  subtree, to produce a deterministic snapshot of the user's currently-
  dirty files with per-file ``+N/-N`` stats. Untracked files are
  flagged separately and their line count is the file's full length
  (since there's nothing in HEAD to diff against).
* :class:`PresenceCache` indexes incoming peer presence events by user
  and applies a freshness window — anything not refreshed within
  ``PRESENCE_FRESHNESS_SECS`` is treated as inactive and dropped from
  the rendered view. Same TTL semantics as the server's ``GET
  /presence`` endpoint, so the local mirror stays in lockstep.

What we deliberately don't ship in v1:

* Live cursor position (line / column) — that requires a per-editor
  extension or LSP server. The path is documented in
  ``spec/PROMPT-LIVE-PLAN.md`` §9; the SSE channel + presence cache
  here is the substrate it would consume.
* Sub-file hunk granularity — ``--numstat`` only gives line counts,
  not ranges. We could parse ``git diff -U0`` for hunk start/end
  lines and ship that next; deferred for now to keep payloads small.

Polling, not filesystem-watching: ``spec watch`` calls
:func:`compute_local_presence` every ``presence_poll_secs`` (default
15s) and broadcasts only when the snapshot has changed since the
previous tick (cheap fingerprint hash). 15s is the sweet spot — fast
enough that "Alice just opened auth.py" feels instant, slow enough
that we're not running git twice a second on every laptop.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .events import IncomingEvent, PresenceFile, PresencePayload

log = logging.getLogger(__name__)

# Mirrored from the backend (``main.py::PRESENCE_FRESHNESS_SECS``).
# We keep them aligned so the local view doesn't disagree with what
# ``GET /presence`` says — a teammate considered "active" by the
# server is also active in your terminal.
PRESENCE_FRESHNESS_SECS = 5 * 60

# Hard caps on how much we ship per snapshot. Mirrored from the
# Pydantic schema so we never serialise something the server will
# 422 on. The "sane refactor" upper bound for files is ~256; "biggest
# blob you'd reasonably edit" for line count is about a million.
MAX_PRESENCE_FILES = 256
MAX_PRESENCE_LINES_PER_FILE = 1_000_000

# Hard cap when counting untracked-file line counts. A new file
# bigger than this is almost certainly machine-generated (a
# fixture, a snapshot, a pasted log); we cap so a 50 MB scratch
# file doesn't stall the watcher.
UNTRACKED_LINE_CAP = 50_000


# ── git plumbing ────────────────────────────────────────────────────


def _run_git(args: list[str], *, cwd: Path) -> str | None:
    """Same ergonomics as ``spec_cli.git._run_git`` — read-only,
    timeout-bound, returns ``None`` on any failure. Re-implemented
    locally to avoid a circular import; kept identical so behaviour
    matches everywhere else in the CLI."""
    if shutil.which("git") is None:
        return None
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout or ""


def _repo_toplevel(bundle_root: Path) -> Path | None:
    out = _run_git(["rev-parse", "--show-toplevel"], cwd=bundle_root)
    if not out:
        return None
    text = out.strip()
    return Path(text) if text else None


def _head_commit(bundle_root: Path) -> str | None:
    out = _run_git(["rev-parse", "HEAD"], cwd=bundle_root)
    if not out:
        return None
    text = out.strip()
    return text or None


def _to_bundle_relative(
    repo_relative: str, *, repo_toplevel: Path, bundle_root: Path
) -> str | None:
    """Convert a repo-root-relative path (what git emits) into a
    bundle-root-relative one. Returns ``None`` for paths outside the
    bundle subtree — what we want for monorepos where ``bundle_root``
    is a child of the worktree.
    """
    abs_path = (repo_toplevel / repo_relative).resolve()
    try:
        rel = abs_path.relative_to(bundle_root.resolve())
    except ValueError:
        return None
    s = str(rel)
    return s if s and s != "." else None


def _count_untracked_lines(path: Path) -> int:
    """Count lines in an untracked file, with a hard cap. Tolerates
    binary files (returns 0) and any IO error — presence is best-effort
    telemetry, never a reason to crash the watcher."""
    try:
        with path.open("rb") as f:
            n = 0
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                if b"\x00" in chunk:
                    return 0  # binary — pretend it's empty
                n += chunk.count(b"\n")
                if n >= UNTRACKED_LINE_CAP:
                    return UNTRACKED_LINE_CAP
            return n
    except OSError:
        return 0


# ── snapshot ────────────────────────────────────────────────────────


@dataclass
class LocalPresence:
    """Result of one ``compute_local_presence`` call.

    ``fingerprint`` is a stable hash of the visible state — used by
    the watcher to debounce broadcasts. Two snapshots with the same
    fingerprint are equivalent for the receiver: same dirty file set,
    same per-file line counts, same head commit. We re-broadcast only
    when this changes.
    """

    files: list[PresenceFile] = field(default_factory=list)
    head_commit: str | None = None
    fingerprint: str = ""

    @property
    def is_clean(self) -> bool:
        return not self.files

    def to_payload(self) -> PresencePayload:
        return PresencePayload(
            files=list(self.files),
            head_commit=self.head_commit,
            is_clean=self.is_clean,
        )


def compute_local_presence(bundle_root: Path) -> LocalPresence:
    """Snapshot the bundle's currently-dirty files via git.

    Returns an empty (``is_clean = True``) ``LocalPresence`` whenever:

    * the directory isn't a git worktree,
    * git is missing,
    * there are no diffs and no untracked files inside ``bundle_root``.

    All three render to the same wire signal — "no presence" — and
    the CLI broadcasts that explicit clean state once so receivers
    can drop the user's row immediately instead of waiting for the
    freshness window.
    """
    bundle_root = bundle_root.resolve()
    repo_toplevel = _repo_toplevel(bundle_root)
    if repo_toplevel is None:
        return LocalPresence(fingerprint=_fingerprint([], None))

    head = _head_commit(bundle_root)

    # --numstat outputs ``<added>\t<removed>\t<path>``; binary files
    # use ``-`` for the counts which we coerce to 0. Renames show
    # up as ``-\t-\told\t{from => to}`` (with ``-z`` we'd avoid
    # quoting; we accept the default since paths_touched on this
    # surface is best-effort and we only emit safe bundle-relative
    # ones anyway).
    diff_out = _run_git(
        ["diff", "--numstat", "HEAD", "--", "."], cwd=bundle_root
    ) or ""
    untracked_out = _run_git(
        ["ls-files", "--others", "--exclude-standard", "--", "."],
        cwd=bundle_root,
    ) or ""

    files: list[PresenceFile] = []

    for line in diff_out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added_raw, removed_raw, path_raw = parts[0], parts[1], parts[2]
        # Rename diff lines have an additional path; the third field
        # holds the from-side and the fourth the to-side. Use the
        # to-side if present so we report the user's *current*
        # filename.
        if len(parts) >= 4 and parts[3]:
            path_raw = parts[3]
        added = _safe_int(added_raw)
        removed = _safe_int(removed_raw)
        if added == 0 and removed == 0:
            continue
        rel = _to_bundle_relative(
            path_raw, repo_toplevel=repo_toplevel, bundle_root=bundle_root
        )
        if rel is None:
            continue
        files.append(
            PresenceFile(
                path=rel,
                lines_added=min(added, MAX_PRESENCE_LINES_PER_FILE),
                lines_removed=min(removed, MAX_PRESENCE_LINES_PER_FILE),
                untracked=False,
            )
        )

    for line in untracked_out.splitlines():
        path_raw = line.strip()
        if not path_raw:
            continue
        rel = _to_bundle_relative(
            path_raw, repo_toplevel=repo_toplevel, bundle_root=bundle_root
        )
        if rel is None:
            continue
        abs_path = (repo_toplevel / path_raw).resolve()
        line_count = _count_untracked_lines(abs_path)
        files.append(
            PresenceFile(
                path=rel,
                lines_added=line_count,
                lines_removed=0,
                untracked=True,
            )
        )

    files.sort(key=lambda f: f.path)
    if len(files) > MAX_PRESENCE_FILES:
        # Truncate deterministically (alphabetical) so a busy refactor
        # doesn't broadcast a different subset on every tick. We also
        # keep the cap symmetric with the server's 422 boundary.
        files = files[:MAX_PRESENCE_FILES]

    return LocalPresence(
        files=files,
        head_commit=head,
        fingerprint=_fingerprint(files, head),
    )


def _safe_int(raw: str) -> int:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _fingerprint(files: list[PresenceFile], head: str | None) -> str:
    """SHA-256 of the visible state, hex-encoded.

    The fingerprint deliberately excludes wall-clock time — we want
    "did anything change since last tick" for debouncing, not "did a
    second pass". Inputs are rendered in a stable, sorted form.
    """
    h = hashlib.sha256()
    h.update((head or "").encode("ascii"))
    h.update(b"\n")
    for f in files:
        h.update(
            f"{f.path}\t{f.lines_added}\t{f.lines_removed}\t{int(f.untracked)}\n".encode(
                "utf-8"
            )
        )
    return h.hexdigest()


# ── peer cache ──────────────────────────────────────────────────────


@dataclass
class PeerPresence:
    """The latest known presence for one teammate.

    Indexed in ``PresenceCache`` by user and installation. ``last_seen`` is the
    time we received the event, used to expire stale entries; the
    server's freshness window is the source of truth, but we apply
    the same window locally so a flapping connection doesn't show
    obviously-stale rows.
    """

    user_id: int
    broadcast_client_id: str | None
    handle: str | None
    name: str
    avatar_url: str | None
    branch: str | None
    head_commit: str | None
    files: list[PresenceFile]
    is_clean: bool
    last_seen: datetime


class PresenceCache:
    """Thread-safe cache of "who's editing what" per installation.

    The receiver thread calls :meth:`apply_event` whenever a presence
    event lands; the watcher's snapshot writer (``team-presence.json``)
    calls :meth:`current` to render. Mutations are coarse-grained
    under one lock — fine because both call sites are infrequent
    relative to the lock cost (one update / 15s per peer; one render
    per change).
    """

    def __init__(
        self,
        *,
        freshness_secs: int = PRESENCE_FRESHNESS_SECS,
        self_user_id: int | None = None,
        self_broadcast_client_id: str | None = None,
    ) -> None:
        self._freshness = timedelta(seconds=freshness_secs)
        self._self_user_id = self_user_id
        self._self_broadcast_client_id = self_broadcast_client_id
        self._by_install: dict[tuple[int, str], PeerPresence] = {}
        self._lock = threading.Lock()

    def apply_event(self, event: IncomingEvent) -> bool:
        """Ingest one ``role = "presence"`` event.

        Returns ``True`` when the cache changed (the writer can
        republish ``team-presence.json``), ``False`` when the event
        was a no-op duplicate / older than what we already have.
        """
        if event.role != "presence":
            return False
        if event.author_user_id <= 0:
            return False
        if (
            self._self_user_id is not None
            and event.author_user_id == self._self_user_id
            and self._self_broadcast_client_id
            and event.broadcast_client_id == self._self_broadcast_client_id
        ):
            # Workspace SSE intentionally echoes this install's own events.
            # Local state is rendered separately as ``self``; caching the echo
            # as a peer creates a false teammate conflict.
            return False
        payload = event.presence
        if payload is None:
            return False
        peer = PeerPresence(
            user_id=event.author_user_id,
            broadcast_client_id=event.broadcast_client_id,
            handle=event.author_handle,
            name=event.author_name,
            avatar_url=event.author_avatar_url,
            branch=event.branch,
            head_commit=payload.head_commit,
            files=list(payload.files),
            is_clean=payload.is_clean,
            last_seen=event.received_at or datetime.now(timezone.utc),
        )
        install_key = (
            peer.user_id,
            peer.broadcast_client_id or "legacy",
        )
        with self._lock:
            existing = self._by_install.get(install_key)
            # Out-of-order delivery is rare on SSE, but possible after
            # a reconnect-driven replay. Keep the newest by wall clock.
            if existing is not None and existing.last_seen > peer.last_seen:
                return False
            if peer.is_clean and not peer.files:
                # Clean-state events are how peers tell us "I'm no
                # longer editing anything"; drop them from the cache
                # so the rendered list is just the actively-editing
                # teammates.
                self._by_install.pop(install_key, None)
                return existing is not None
            self._by_install[install_key] = peer
            return True

    def expire_stale(self) -> int:
        """Drop entries whose ``last_seen`` is older than the
        freshness window. Returns the number of entries removed.
        Called periodically by the watcher's mirror loop so the
        local view doesn't show ghost teammates after a network
        partition or a peer's laptop sleeping."""
        cutoff = datetime.now(timezone.utc) - self._freshness
        with self._lock:
            stale = [key for key, p in self._by_install.items() if p.last_seen < cutoff]
            for key in stale:
                self._by_install.pop(key, None)
            return len(stale)

    def current(self) -> list[PeerPresence]:
        """Snapshot of every active peer's presence, sorted by
        ``last_seen`` desc so the most recently-active teammate
        renders first."""
        self.expire_stale()
        with self._lock:
            peers = list(self._by_install.values())
        peers.sort(key=lambda p: p.last_seen, reverse=True)
        return peers

    def replace_all(self, events: list[IncomingEvent]) -> None:
        """Used by ``spec watch`` on connect: replace the cache with
        the server's authoritative ``GET /presence`` snapshot. Any
        local rows older than the snapshot are discarded."""
        with self._lock:
            self._by_install.clear()
        for evt in events:
            self.apply_event(evt)

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_install)


__all__ = [
    "MAX_PRESENCE_FILES",
    "MAX_PRESENCE_LINES_PER_FILE",
    "PRESENCE_FRESHNESS_SECS",
    "UNTRACKED_LINE_CAP",
    "LocalPresence",
    "PeerPresence",
    "PresenceCache",
    "compute_local_presence",
]
