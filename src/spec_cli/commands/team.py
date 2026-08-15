"""
``spec team`` — recent prompt activity (snapshot) plus subcommands for
the live workspace-wide stream and flagging teammates' prompts.

Subcommands:

* ``spec team`` — print recent prompt events (snapshot from the REST
  list endpoints). Bundle-scoped by default; ``--org`` falls back to
  ``GET /api/me/prompt-events`` for a workspace-wide listing.
* ``spec team watch`` — long-lived SSE tail across every bundle the
  caller can see (``GET /api/me/prompt-stream``). Receive-only;
  designed to live in a dedicated terminal so engineers can watch
  every running agent on every project from one screen.
* ``spec team flag <event_id> --kind …`` — post a flag (reaction /
  warning / question / ack) on a prompt event. The flag fans out
  over the same SSE channel so peers see it within an RTT.
* ``spec team request-push <handle>`` — append a git-push handoff row
  to ``.spec/team-push-requests.yaml`` (merged into team-presence /
  editing-brief by ``spec watch``). Same intent as ``/push@handle`` in
  ``spec team watch`` when your cwd is the bundle root.
"""
from __future__ import annotations

import json
import os
import queue
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone

from pathlib import Path

import click

from ..api import ApiError, CloudClient
from ..config import (
    BundleNotFoundError,
    RemoteUrlError,
    find_bundle_root,
    load_credentials,
    load_manifest,
    parse_cloud_project,
)
from ..git import read_git_context
from ..realtime.broadcast_identity import load_or_create_broadcast_client_id
from ..realtime.live_event_dedup import LivePromptEventDeduper
from ..realtime.commands import (
    CommandContext,
    WatchState,
    dispatch,
    make_buffer,
    parse_command,
)
from ..realtime.critic import (
    critique_event,
    is_tool_only_summary,
    suggested_flag_command,
)
from ..realtime.events import IncomingEvent, IncomingFlag
from ..realtime.merge_turns import merge_assistant_snapshots
from ..realtime.notifier import Notifier, WORKSPACE_FEED_LABEL
from ..realtime.team_push_requests import (
    DEFAULT_PUSH_REQUEST_TTL_SECS,
    record_push_request,
)
from ..realtime.transport import SSEConsumer, SSEStreamError, run_consumer_in_thread
from ..ui import configure_streaming_stdio, console, dim, fatal, ok


def _ago(value: datetime | None) -> str:
    if value is None:
        return "?"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    seconds = max(0, (datetime.now(timezone.utc) - value).total_seconds())
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86_400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86_400)}d ago"


# GitHub-style handle — server ``author_handle`` filter is exact match.
_HANDLE_STYLE = re.compile(r"^[a-z0-9][a-z0-9-]{0,37}$")

# Bundle-scoped list has no ``role=`` query param — when the user passes
# ``--role user``, over-fetch before client-side filter so presence rows
# do not hide real turns behind a small ``--limit``.
_TEAM_SNAPSHOT_ROLE_OVERFETCH_MAX = 200

# Closed enum (mirrors the server's ``PromptEventFlagCreate``).
_FLAG_KINDS = ("warning", "question", "block", "ack")


def _event_matches_user_filter(ev: IncomingEvent, needle: str) -> bool:
    n = needle.lower().strip().lstrip("@")
    if not n:
        return True
    handle = (ev.author_handle or "").lower()
    name = (ev.author_name or "").lower()
    display = ev.author_display.lower()
    return n in handle or n in name or n in display


def _run_team_snapshot(
    limit: int,
    branch_filter: str | None,
    role_filter: str | None,
    project: str | None,
    org_wide: bool,
    user_filter: str | None,
    *,
    include_presence: bool,
) -> None:
    creds = load_credentials()
    if not creds or not creds.access_token:
        fatal("Not signed in. Run `spec login` first.")
        return

    client = CloudClient(creds)
    label: str

    if org_wide:
        api_author = None
        if user_filter:
            cand = user_filter.strip().lstrip("@").lower()
            if cand and _HANDLE_STYLE.match(cand):
                api_author = cand
        try:
            rows = client.list_my_prompt_events(
                limit=limit,
                author_handle=api_author,
                role=role_filter,
                include_presence=include_presence,
            )
        except ApiError as e:
            fatal(str(e))
            return
        label = "workspace (all your bundles)"
    else:
        try:
            root = find_bundle_root()
        except BundleNotFoundError as e:
            fatal(str(e))
            return
        manifest = load_manifest(root)
        raw = project or manifest.cloud_project
        if not raw:
            fatal(
                "No cloud project configured. Add `cloud.project: <handle>/<slug>` "
                "to spec.yaml, pass --project, or use --org for a workspace-wide feed."
            )
            return
        try:
            handle, slug = parse_cloud_project(raw, default_handle=creds.user_handle)
        except RemoteUrlError as e:
            fatal(str(e))
            return
        try:
            project_info = client.resolve_project(handle, slug)
        except ApiError as e:
            fatal(str(e))
            return
        project_id = int(project_info["id"])
        fetch_limit = limit
        if role_filter:
            fetch_limit = min(
                _TEAM_SNAPSHOT_ROLE_OVERFETCH_MAX,
                max(limit * 25, 50),
            )
        try:
            rows = client.list_prompt_events(project_id, limit=fetch_limit)
        except ApiError as e:
            fatal(str(e))
            return
        label = f"{handle}/{slug}"

    events = [IncomingEvent.from_json(r) for r in rows if isinstance(r, dict)]
    events = [e for e in events if e.role != "assistant_closed"]
    # Bundle-scoped REST returns presence pings (``spec watch`` ~15s) mixed
    # with real turns. They used to render as fake ``AI … assistant``
    # rows because ``model`` is null — crowding ``--limit`` and hiding
    # actual Cursor / Claude activity. Match ``--org`` defaults: hide
    # unless the user explicitly asks.
    if not include_presence:
        events = [e for e in events if e.role != "presence"]

    if branch_filter:
        needle = branch_filter.lower()
        events = [e for e in events if e.branch and needle in e.branch.lower()]
    if role_filter and not org_wide:
        events = [e for e in events if e.role == role_filter]
    if user_filter:
        events = [e for e in events if _event_matches_user_filter(e, user_filter)]

    events = events[:limit]

    if not events:
        dim(f"no recent activity for {label} — waiting for the team.")
        return

    scope = "[sf.label]team activity (org)[/]" if org_wide else "[sf.label]team activity[/]"
    console.print(
        f"{scope} [bold]{label}[/] [sf.muted]· {len(events)} event(s)[/]"
    )
    for event in events:
        when = _ago(event.turn_at or event.received_at)
        author = event.author_display
        branch = event.branch or "-"
        bundle = ""
        if event.bundle_label:
            bundle = f" [sf.muted]· {event.bundle_label}[/]"
        # Same role-badge convention as the live watcher so muscle
        # memory transfers between `spec team` and `spec team watch`.
        # Source color tags help separate concurrent claude_code /
        # codex / cursor activity in the snapshot too.
        if event.role == "user":
            badge = "[bold black on #3ddab4] USER [/]"
            who = f"[bold #3ddab4]{author}[/]"
        elif event.role == "presence":
            badge = "[bold black on #9aa3b2] PRS [/]"
            who = f"[bold #c7c9d1]{author}[/]"
        else:
            badge = "[bold black on #7de3ff]  AI  [/]"
            model = event.model or "assistant"
            who = (
                f"[bold #7de3ff]{model}[/] [sf.muted]→[/] "
                f"[bold #3ddab4]{author}[/]"
            )
        src_color = {
            "claude_code": "#c79bff",
            "codex": "#9ee37d",
            "cursor": "#7de3ff",
            "manual": "#c7c9d1",
        }.get(event.source, "#9aa3b2")
        src = f"[bold {src_color}]{event.source}[/]"
        head = (
            f"  {badge} [sf.muted]#{event.id}[/] {who} "
            f"[sf.muted]· {branch} · {when} · in[/] {src}"
            f"{bundle}"
        )
        console.print(head)
        raw = (event.text or event.summary or "").strip()
        if raw:
            if event.role == "assistant":
                # Same preference as :class:`Notifier` — ``summary`` is a
                # headline; ``text`` carries the reply reviewers skim for.
                budget, max_lines = 96_000, 120
            else:
                budget, max_lines = 48_000, 80
            lines: list[str] = []
            used = 0
            for line in raw.splitlines():
                if not line.strip() and not lines:
                    continue
                if len(lines) >= max_lines:
                    lines.append("…")
                    break
                if used + len(line) > budget:
                    remain = max(0, budget - used - 1)
                    if remain > 8:
                        lines.append(line[:remain].rstrip() + "…")
                    else:
                        lines.append("…")
                    break
                lines.append(line)
                used += len(line) + 1
            for ln in lines:
                console.print(f"      [sf.muted]{ln}[/]")
        # Apply the same auto-critic in the snapshot view so a `spec
        # team` glance flags the same risky prompts the live watcher
        # would. Cheap (pure regex) and only fires on user turns.
        for c in critique_event(event):
            console.print(
                f"      [{c.color}]{c.glyph} AUTO {c.rule}[/] "
                f"[sf.muted]{c.msg}[/]"
            )
            console.print(
                f"        [sf.muted]→ {suggested_flag_command(event.id, c)}[/]"
            )


@click.group(name="team", invoke_without_command=True)
@click.pass_context
@click.option(
    "--limit",
    "-n",
    "limit",
    default=20,
    show_default=True,
    type=click.IntRange(1, 200),
    help="Number of recent events to show (snapshot only).",
)
@click.option(
    "--branch",
    "branch_filter",
    default=None,
    help="Only show events on this branch. Substring match (case-insensitive).",
)
@click.option(
    "--role",
    "role_filter",
    type=click.Choice(["user", "assistant"]),
    default=None,
    help="Only show user or assistant turns.",
)
@click.option(
    "--project",
    "-p",
    default=None,
    help="Override `cloud.project` from spec.yaml (ignored with `--org`).",
)
@click.option(
    "--org",
    "org_wide",
    is_flag=True,
    help=(
        "Workspace-wide feed: all bundles you can see on Spec Cloud, "
        "one API round trip (`GET /api/me/prompt-events`). Does not "
        "require standing inside a bundle directory."
    ),
)
@click.option(
    "--user",
    "user_filter",
    default=None,
    help=(
        "Only events from this teammate — matches handle (substring), "
        "display name, or @handle (case-insensitive)."
    ),
)
@click.option(
    "--include-presence",
    "include_presence",
    is_flag=True,
    default=False,
    help=(
        "Include ``role=presence`` rows (git dirty-file pings from "
        "``spec watch``). Off by default — they are noisy and were easy "
        "to mistake for AI turns before this flag existed."
    ),
)
def team_group(
    ctx: click.Context,
    limit: int,
    branch_filter: str | None,
    role_filter: str | None,
    project: str | None,
    org_wide: bool,
    user_filter: str | None,
    include_presence: bool,
) -> None:
    """Print recent Spec Live prompt activity, or stream the whole workspace.

    Default (no subcommand): snapshot from the REST list endpoints.

    \b
    Examples:
      spec team
      spec team --org --limit 50
      spec team --user alice
      spec team request-push jc -m "need your branch"
      spec team watch
      spec team flag 4711 --kind warning --note "race condition risk"
    """
    if ctx.invoked_subcommand is not None:
        return
    _run_team_snapshot(
        limit,
        branch_filter,
        role_filter,
        project,
        org_wide,
        user_filter,
        include_presence=include_presence,
    )


# Idle interval (seconds) between visible "still watching" heartbeats
# in `spec team watch`. Chosen so a quiet workspace still feels alive
# without ever competing with real events for screen real estate.
_TEAM_WATCH_HEARTBEAT_SECS = 60.0
# Workspace SSE only replays when ``Last-Event-ID`` is set. On a fresh
# connect that cursor is empty — so we prime the pane from REST once
# (same bundles as the stream) and then resume the socket from the
# newest id so reviewers still see the user prompt that kicked off a
# thread they joined mid-flight. Keep this comfortably above bursty
# assistant-only streaks (Claude Code JSONL) so the USER row is still
# in the warm-up batch.
# Workspace bootstrap before SSE: most recent N events across bundles.
# Large enough that joining ``spec team watch`` mid-thread still shows
# the USER row that kicked off the current reply (Codex/Cursor can emit
# long assistant-only tails). Cap keeps the first REST round trip bounded.
_TEAM_WATCH_BOOTSTRAP_LIMIT = 200
_TEAM_WATCH_BOOTSTRAP_USER_SLOTS = 25


def _build_team_watch_bootstrap_events(
    client: CloudClient,
    *,
    limit: int,
    include_presence: bool,
) -> list[IncomingEvent]:
    """Merge recent user prompts with the latest tail for warm-up.

    ``GET /api/me/prompt-events`` returns newest-first; a live Codex run
    can fill the whole window with assistant rows. We always pull recent
    ``role=user`` rows separately, then cap the combined set at ``limit``.
    """
    user_cap = min(_TEAM_WATCH_BOOTSTRAP_USER_SLOTS, limit)
    other_cap = max(0, limit - user_cap)

    user_rows = client.list_my_prompt_events(
        limit=user_cap,
        role="user",
        include_presence=include_presence,
    )
    tail_rows = client.list_my_prompt_events(
        limit=limit,
        include_presence=include_presence,
    )

    by_id: dict[int, IncomingEvent] = {}
    for raw in user_rows + tail_rows:
        if not isinstance(raw, dict):
            continue
        ev = IncomingEvent.from_json(raw)
        if ev is not None and ev.id >= 0:
            by_id[ev.id] = ev

    users = [e for e in by_id.values() if e.role == "user"]
    users.sort(key=lambda e: e.id)
    users = users[-user_cap:]

    others = [
        e
        for e in by_id.values()
        if e.role != "user" and e.id not in {u.id for u in users}
    ]
    others.sort(key=lambda e: e.id)
    others = others[-other_cap:]

    return sorted(users + others, key=lambda e: e.id)

# Default idle flush when ``assistant_closed`` is delayed (POST blips,
# broadcaster on an older CLI, or a long tail-stability window). Zero
# still means "wait for ``assistant_closed`` only" — set
# ``--assistant-quiet-secs 0`` or ``SPEC_TEAM_WATCH_ASSISTANT_QUIET_SECS=0``.
_TEAM_WATCH_ASSISTANT_QUIET_SECS_DEFAULT = 60.0


def _assistant_has_reviewable_prose(event: IncomingEvent) -> bool:
    """False for Cursor's ``[REDACTED]``-only snapshots (tool steps).

    Those rows carry structured ``tool_calls`` but no human-readable prose;
    showing them as live "AI" lines spammed the pane with useless headers.

    Must match :meth:`Notifier._assistant_visible_prose` — a row with
    ``text="[REDACTED]"`` and a real ``summary`` is reviewable, but the
    old check treated raw ``text`` as prose and printed only ``[REDACTED]``.
    """
    if not Notifier._assistant_preview_is_meaningful(event.text, event.summary):
        return False
    body = Notifier._assistant_visible_prose(event.text, event.summary).strip()
    return not is_tool_only_summary(body)


def _resolve_assistant_quiet_secs(cli_value: float | None) -> float:
    """CLI wins, then env ``SPEC_TEAM_WATCH_ASSISTANT_QUIET_SECS``, then default.

    ``0`` disables idle-based flush (pair on next user, error, ``/pair``, or exit).
    """
    if cli_value is not None:
        return max(0.0, float(cli_value))
    raw = (os.environ.get("SPEC_TEAM_WATCH_ASSISTANT_QUIET_SECS") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return _TEAM_WATCH_ASSISTANT_QUIET_SECS_DEFAULT


# ``GET /api/projects/{id}/prompt-events?since_id=`` hard cap for one
# Q/A merge — matches the backend list ceiling order of magnitude.
_TEAM_WATCH_THREAD_FETCH_LIMIT = 2500
# Shorter than the generic Cloud client default so a wedged API cannot
# stall the team-watch main loop for half a minute on every merge.
_TEAM_WATCH_THREAD_FETCH_TIMEOUT_SECS = 28.0


def _user_from_rest_before_assistant(
    client: CloudClient | None,
    ev: IncomingEvent,
) -> IncomingEvent | None:
    """Latest ``role=user`` row for this session with ``id < ev.id``.

    Heals SSE ordering gaps where assistant snapshots arrive before the
    matching user prompt (or the user row was never streamed live).
    """
    if client is None or ev.role != "assistant":
        return None
    sid = (ev.session_id or "").strip()
    if not sid:
        return None
    window = 120
    since = max(0, ev.id - window)
    try:
        rows = client.list_prompt_events(
            ev.project_id,
            since_id=since,
            limit=window + 20,
            timeout=_TEAM_WATCH_THREAD_FETCH_TIMEOUT_SECS,
        )
    except ApiError:
        return None
    key = (ev.project_id, sid)
    best: IncomingEvent | None = None
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            row = IncomingEvent.from_json(r)
        except (KeyError, TypeError, ValueError):
            continue
        if (row.project_id, (row.session_id or "").strip()) != key:
            continue
        if row.role != "user" or row.id >= ev.id:
            continue
        if best is None or row.id > best.id:
            best = row
    return best


def _assistant_tail_from_rest_after_user(
    client: CloudClient,
    pending: IncomingEvent,
) -> list[IncomingEvent]:
    """Assistant rows persisted after ``pending`` for server-backed merge.

    Wraps the project catch-up list (``id`` ascending) and stops at the
    next ``user`` event so a later prompt in the same project cannot
    attach to this thread. Rows from other ``session_id`` values are
    skipped so interleaved workspace traffic does not corrupt the pair.
    """
    try:
        rows = client.list_prompt_events(
            pending.project_id,
            since_id=pending.id,
            limit=_TEAM_WATCH_THREAD_FETCH_LIMIT,
            timeout=_TEAM_WATCH_THREAD_FETCH_TIMEOUT_SECS,
        )
    except ApiError:
        return []
    key = (pending.project_id, (pending.session_id or "").strip())
    out: list[IncomingEvent] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            ev = IncomingEvent.from_json(r)
        except (KeyError, TypeError, ValueError):
            continue
        if (ev.project_id, (ev.session_id or "").strip()) != key:
            continue
        if ev.role == "user":
            break
        if ev.role == "assistant":
            out.append(ev)
    return out


class _TeamWatchQAState:
    """Coalesce streaming assistant rows into one readable Q/A block.

    The user always sees their ``USER`` row immediately; assistant
    chunks merge until a flush boundary: ``assistant_closed`` from the
    broadcaster's ``spec watch`` (tail stability), the next user
    message, ``error``, ``/pair``, process exit, or (unless
    ``--assistant-quiet-secs 0``) idle quiet after the last assistant
    chunk (or after the user prompt landed when only Cloud carries
    assistant rows). Assistant rows must match the pending user's ``project_id``
    and ``session_id`` so a workspace-wide stream cannot attach another
    thread's reply — see :meth:`Notifier.show_completed_pair`.
    """

    def __init__(self) -> None:
        self.pending_user: IncomingEvent | None = None
        self.assistant_chunks: list[IncomingEvent] = []
        self.last_assistant_mono: float | None = None
        # ``time.monotonic()`` when :attr:`pending_user` was set — used by
        # :meth:`tick_quiet_flush` when no SSE assistant chunk ever
        # arrived but Cloud already has rows (``combined_assistant_chunks``).
        self.pending_since_mono: float | None = None
        # Optional ``CloudClient`` — when set, :meth:`flush_pair` merges
        # SSE-buffered assistant rows with the durable project tail from
        # REST so ``/pair`` and idle flush see the same text the server
        # stored (heals missed SSE frames and early ``/pair`` races).
        self.pair_cloud: CloudClient | None = None
        # After ``show_completed_pair``, suppress stray assistant rows for the
        # same (project, session) with ``id`` not above this tail (duplicate
        # SSE / late snapshots that would otherwise hit ``notifier.show``).
        self._closed_assistant_hi: dict[tuple[int, str], int] = {}
        # After ``show_completed_pair``, suppress re-printing the same
        # *user prompt row* (same Cloud ``id``) if the stream redelivers it.
        self._seen_user_event_ids: set[tuple[int, int]] = set()
        # One merged block per (project, session, user event id) — keyed
        # by the durable user row id, not prompt body text, so two real
        # prompts that say "ok" twice still both render.
        self._flushed_pair_keys: set[tuple[int, str, int]] = set()

    def _bump_assistant_hi_after_flush(
        self, pending: IncomingEvent, combined: list[IncomingEvent]
    ) -> None:
        if not combined:
            return
        key = self._session_key(pending)
        hi = max(c.id for c in combined)
        prev = self._closed_assistant_hi.get(key, 0)
        if hi > prev:
            self._closed_assistant_hi[key] = hi

    def should_suppress_lagging_assistant(self, ev: IncomingEvent) -> bool:
        """True when this assistant row is at/below the last merged tail."""
        if ev.role != "assistant":
            return False
        if self.pending_user is not None:
            return False
        key = self._session_key(ev)
        hi = self._closed_assistant_hi.get(key)
        if hi is None:
            return False
        return ev.id <= hi

    @staticmethod
    def _session_key(ev: IncomingEvent) -> tuple[int, str]:
        return (ev.project_id, (ev.session_id or "").strip())

    @staticmethod
    def _pair_flush_key(pending: IncomingEvent) -> tuple[int, str, int]:
        return (
            pending.project_id,
            (pending.session_id or "").strip(),
            pending.id,
        )

    def is_duplicate_user(self, ev: IncomingEvent) -> bool:
        """True when this exact user row was already shown in this watch run."""
        if ev.role != "user":
            return False
        key = (ev.project_id, ev.id)
        if key in self._seen_user_event_ids:
            return True
        self._seen_user_event_ids.add(key)
        return False

    @staticmethod
    def _merge_assistant_chunks(chunks: list[IncomingEvent]) -> IncomingEvent:
        return merge_assistant_snapshots(chunks)

    def combined_assistant_chunks(self) -> list[IncomingEvent]:
        """Local SSE buffer plus assistant rows from REST (when configured).

        Deduplicates by monotonic ``id`` so the merged assistant body
        prefers every snapshot the broadcaster successfully POSTed,
        even when the live SSE consumer missed intermediate frames.
        """
        if self.pending_user is None:
            return []
        key = self._session_key(self.pending_user)
        local = [
            c
            for c in self.assistant_chunks
            if c.role == "assistant" and self._session_key(c) == key
        ]
        remote: list[IncomingEvent] = []
        if self.pair_cloud is not None:
            remote = _assistant_tail_from_rest_after_user(
                self.pair_cloud, self.pending_user
            )
        by_id: dict[int, IncomingEvent] = {}
        for ev in sorted(remote + local, key=lambda e: e.id):
            by_id[ev.id] = ev
        return list(by_id.values())

    def flush_pair(self, notifier: Notifier) -> bool:
        if self.pending_user is None:
            return False
        pair_key = self._pair_flush_key(self.pending_user)
        if pair_key in self._flushed_pair_keys:
            self.assistant_chunks.clear()
            self.pending_user = None
            self.last_assistant_mono = None
            self.pending_since_mono = None
            return False
        combined = self.combined_assistant_chunks()
        if not combined:
            return False
        merged = self._merge_assistant_chunks(combined)
        self._flushed_pair_keys.add(pair_key)
        self._bump_assistant_hi_after_flush(self.pending_user, combined)
        notifier.show_completed_pair(self.pending_user, merged)
        self.assistant_chunks.clear()
        self.pending_user = None
        self.last_assistant_mono = None
        self.pending_since_mono = None
        return True

    def on_user(
        self,
        ev: IncomingEvent,
        notifier: Notifier,
        last_output_at: list[float],
        *,
        is_bootstrap: bool = False,
    ) -> None:
        if not is_bootstrap and self.is_duplicate_user(ev):
            return
        if self.pending_user is not None and self.combined_assistant_chunks():
            self.flush_pair(notifier)
        self.assistant_chunks.clear()
        self.last_assistant_mono = None
        self.pending_user = ev
        self.pending_since_mono = time.monotonic()
        notifier.show(ev)
        last_output_at[0] = time.monotonic()

    def buffer_assistant(self, ev: IncomingEvent) -> bool:
        if self.pending_user is None:
            return False
        if self._session_key(ev) != self._session_key(self.pending_user):
            return False
        self.assistant_chunks.append(ev)
        self.last_assistant_mono = time.monotonic()
        return True

    def flush_on_assistant_closed(
        self,
        ev: IncomingEvent,
        notifier: Notifier,
        last_output_at: list[float],
    ) -> bool:
        """Flush when ``spec watch`` posts ``role=assistant_closed`` for this session."""
        if ev.role != "assistant_closed":
            return False
        if self.pending_user is None:
            return False
        # Match :meth:`buffer_assistant` — normalize ``session_id`` so a
        # stray whitespace mismatch does not strand buffered chunks.
        if self._session_key(self.pending_user) != self._session_key(ev):
            return False
        combined = self.combined_assistant_chunks()
        if not combined:
            return False
        cid = ev.closes_event_id
        if cid is not None:
            a_ids = [c.id for c in combined]
            lo, hi = min(a_ids), max(a_ids)
            if cid < lo or cid > hi:
                return False
        if not self.flush_pair(notifier):
            return False
        last_output_at[0] = time.monotonic()
        return True

    def tick_quiet_flush(
        self,
        notifier: Notifier,
        last_output_at: list[float],
        quiet_secs: float,
    ) -> None:
        # ``0`` = never flush on idle — wait for next user message, error,
        # or shutdown so a single turn can run arbitrarily long (hours).
        if quiet_secs <= 0:
            return
        if self.pending_user is None:
            return
        if not self.combined_assistant_chunks():
            return
        # Anchor idle detection on the last SSE assistant chunk when we
        # have one; otherwise on the user prompt arrival time so a
        # Cloud-only tail (missed SSE) still becomes eligible for flush.
        anchor = self.last_assistant_mono
        if anchor is None:
            anchor = self.pending_since_mono
        if anchor is None:
            return
        if time.monotonic() - anchor < quiet_secs:
            return
        if self.flush_pair(notifier):
            last_output_at[0] = time.monotonic()

    def flush_on_error(self, notifier: Notifier, last_output_at: list[float]) -> None:
        if self.pending_user is not None and self.combined_assistant_chunks():
            self.flush_pair(notifier)
            last_output_at[0] = time.monotonic()
        self.pending_user = None
        self.assistant_chunks.clear()
        self.last_assistant_mono = None
        self.pending_since_mono = None

    def flush_shutdown(self, notifier: Notifier) -> None:
        if self.pending_user is None:
            self.assistant_chunks.clear()
            self.last_assistant_mono = None
            self.pending_since_mono = None
            return
        combined = self.combined_assistant_chunks()
        if not combined:
            self.pending_user = None
            self.assistant_chunks.clear()
            self.last_assistant_mono = None
            self.pending_since_mono = None
            return
        merged = self._merge_assistant_chunks(combined)
        self._bump_assistant_hi_after_flush(self.pending_user, combined)
        notifier.show_completed_pair(self.pending_user, merged)
        self.pending_user = None
        self.assistant_chunks.clear()
        self.last_assistant_mono = None
        self.pending_since_mono = None


def _stdin_is_interactive() -> bool:
    """Whether ``sys.stdin`` looks like an interactive TTY.

    We refuse to start the slash-command reader otherwise — piping a
    log file into ``spec team watch`` should not silently start
    interpreting log lines as commands. Anything that fails the
    isatty() check (CI runners, ``< /dev/null``, ``screen -L``
    rotated buffers) falls back to read-only mode.
    """
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


def _stdin_reader(ctx: "CommandContext", stop_event: threading.Event) -> None:
    """Read slash-commands from stdin until ``stop_event`` fires.

    Lives in a daemon thread so a hung ``readline()`` does not block
    process exit after Ctrl+C — the kernel reaps stdin when the main
    thread tears down. Non-command lines are silently ignored, which
    means a reviewer who fat-fingers their editor open in the same
    pane doesn't accidentally trigger anything destructive.
    """
    while not stop_event.is_set():
        try:
            line = sys.stdin.readline()
        except (KeyboardInterrupt, ValueError):
            return
        if not line:
            # EOF on stdin (Ctrl+D, or piped input exhausted) — let
            # the watcher continue running purely as a stream
            # consumer; we just stop accepting commands.
            return
        cmd = parse_command(line)
        if cmd is None:
            continue
        dispatch(cmd, ctx)


@team_group.command("request-push")
@click.argument("handle", type=str)
@click.option(
    "--message",
    "-m",
    default=None,
    help="Optional note shown to the target's AI in the handoff YAML.",
)
@click.option(
    "--ttl",
    type=click.IntRange(60, 86400),
    default=DEFAULT_PUSH_REQUEST_TTL_SECS,
    show_default=True,
    help="Seconds before this request expires from the mirror files.",
)
def team_request_push_cmd(handle: str, message: str | None, ttl: int) -> None:
    """Ask a teammate (by Spec handle) to git-push — shared YAML + mirror.

    Writes ``.spec/team-push-requests.yaml`` in the current bundle. With
    ``spec watch`` running on teammates' machines, the row is merged into
    ``team-presence.json`` and ``team-editing-brief.md`` so their AI tools
    see the handoff (same as ``/push@handle`` inside ``spec team watch``
    when cwd is the bundle root).

    \b
    Examples:
      spec team request-push jc
      spec team request-push jc -m "need your WIP for integration"
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return
    creds = load_credentials()
    fh = (creds.user_handle or "").strip().lstrip("@").lower() if creds else None
    disp: str | None = None
    if creds:
        if creds.user_handle:
            disp = f"@{creds.user_handle.lstrip('@')}"
        if creds.user_name:
            disp = f"{creds.user_name} ({disp})" if disp else creds.user_name
    git = read_git_context(root)
    try:
        path = record_push_request(
            root,
            to_handle=handle,
            from_handle=fh or None,
            from_display=disp,
            branch=git.branch,
            message=message,
            ttl_secs=int(ttl),
        )
    except ValueError as e:
        fatal(str(e))
        return
    except OSError as e:
        fatal(str(e))
        return
    ok(
        f"recorded push handoff for @{handle.lstrip('@').lower()} → {path}\n"
        "Teammates pick it up on the next `spec watch` mirror tick (≤30s) "
        "if their daemon is running."
    )


@team_group.command("watch")
@click.option(
    "--compact",
    is_flag=True,
    help="One line per event instead of the multi-line default.",
)
@click.option(
    "--include-presence",
    is_flag=True,
    help="Include presence pings in the stream (very noisy).",
)
@click.option(
    "--heartbeat/--no-heartbeat",
    "heartbeat",
    default=True,
    show_default=True,
    help=(
        "Print a single `· still watching ·` line on idle so the terminal "
        "never looks frozen. Disable in dashboards / CI to keep the log clean."
    ),
)
@click.option(
    "--heartbeat-interval",
    type=click.IntRange(15, 3600),
    default=int(_TEAM_WATCH_HEARTBEAT_SECS),
    show_default=True,
    help="Seconds between heartbeat lines when --heartbeat is on.",
)
@click.option(
    "--critic/--no-critic",
    "critic_enabled",
    default=True,
    show_default=True,
    help=(
        "Run the rule-based auto-critic against every user prompt and "
        "print suggestions inline. Each suggestion includes the exact "
        "`spec team flag` command to escalate it into a team-visible flag."
    ),
)
@click.option(
    "--verbose/--no-verbose",
    "verbose",
    default=True,
    show_default=True,
    help=(
        "Receive full assistant ``text`` bodies (default). Use "
        "``--no-verbose`` for a summary-only feed: assistant turns "
        "show only the short summary line, user prompts are still "
        "shipped in full so reviewers can see what triggered each "
        "response."
    ),
)
@click.option(
    "--show-tool-runs/--no-tool-runs",
    "show_tool_runs",
    default=False,
    show_default=True,
    help=(
        "Expand each assistant turn's structured ``tool_calls`` list "
        "under the prose body (``Edit auth.py``, ``Bash \"pytest -q\"``, "
        "``Read main.py``…), and keep fenced code blocks in the prose "
        "intact instead of collapsing them to ``[code: lang ~N lines]``. "
        "Off by default — the default pane shows full AI narration "
        "without code or tool spam so two teammates' threads stay "
        "scannable. The auto-critic still inspects every tool call "
        "even when this flag is off."
    ),
)
@click.option(
    "--commands/--no-commands",
    "commands_enabled",
    default=True,
    show_default=True,
    help=(
        "Enable the in-pane slash-command layer (default). Disable for "
        "fully passive read-only mode. Commands include /summarize, "
        "/flag, /focus, /mute, /replay, /search, /turn, /full, /push, "
        "/critic, /status, /help."
    ),
)
@click.option(
    "--notify/--no-notify",
    "notify",
    default=False,
    show_default=True,
    help=(
        "Ring the terminal bell and (on macOS) fire a system "
        "notification banner when the auto-critic catches a "
        "block-severity hit on a teammate's turn — destructive "
        "command, leaked secret, test bypass. Off by default; turn "
        "on when you can't keep eyes on the pane."
    ),
)
@click.option(
    "--assistant-quiet-secs",
    type=float,
    default=None,
    help=(
        "Seconds with no new assistant chunk before printing the paired "
        "Q/A block without another user message. Default 60 (fallback when "
        "assistant_closed is delayed). Use 0 to wait only for "
        "assistant_closed, the next user, error, /pair, or exit. Override "
        "with SPEC_TEAM_WATCH_ASSISTANT_QUIET_SECS."
    ),
)
def team_watch_cmd(
    compact: bool,
    include_presence: bool,
    heartbeat: bool,
    heartbeat_interval: int,
    critic_enabled: bool,
    verbose: bool,
    show_tool_runs: bool,
    commands_enabled: bool,
    notify: bool,
    assistant_quiet_secs: float | None,
) -> None:
    """Live SSE tail across every bundle you can see (workspace-wide).

    Connects to ``GET /api/me/prompt-stream``. Receive-only — does not
    require a bundle directory. The SSE reader thread only enqueues
    frames; the **main thread** runs delivery, Q/A merge, and Cloud
    catch-up fetches so a slow ``GET /prompt-events`` never blocks the
    socket. Reconnects with exponential backoff on transient drops;
    ``Ctrl+C`` once asks for a graceful exit, a second press forces.

    **Q/A coalescing (REST warm-up + live SSE):** each user prompt is
    printed as soon as it arrives. Assistant chunks merge until a flush
    boundary: an ``assistant_closed`` row from the teammate's
    ``spec watch`` (when their tail assistant bubble stabilizes), the
    next user message, an ``error`` row, ``/pair``, process exit, or
    (unless ``--assistant-quiet-secs 0``) that many idle seconds after
    the last SSE assistant chunk — or after the user prompt when only
    Cloud holds assistant rows the stream missed. The idle window
    remains a fallback when the
    broadcaster runs an older CLI that does not emit ``assistant_closed``.
    The paired block merges **SSE-buffered assistant rows with the
    durable project tail** from ``GET /api/projects/{id}/prompt-events``
    (monotonic ids) so reviewers see the full stored reply even when the
    live stream missed frames or ``/pair`` ran early. **Non-compact**
    default layout keeps **user** bodies up to the schema wire cap while
    **assistant** prose is capped (~400 characters) after merging
    ``text`` and ``summary`` so the headline is not shown without the
    body. Use ``/turn`` or ``/full`` (session chip from ``● turn complete``)
    to print full stored turns from Cloud; ``--show-tool-runs`` lifts the
    live assistant cap to the full stored body and shows tools + code.
    The initial REST warm-up uses the same merge rules.

    Two reviewer aids run automatically and can be disabled if the
    output ever gets noisy:

    * **Auto-critic** — every user prompt is matched against a small
      catalogue of "AI is about to do something dangerous" rules
      (destructive verbs, test-bypass language, vague intent, leaked
      secrets). Each firing rule prints one suggestion line plus the
      exact ``spec team flag`` command to escalate it. Turn off with
      ``--no-critic``.

    * **No-reply hint** — if a user prompt has been visible for 90s+
      and no assistant turn from the same session has arrived, the
      watcher surfaces a `⏳ no-reply` line. Catches the common case
      where a teammate's broadcaster is sharing prompts but not
      assistant text (the AI looks "silent" when really we just
      aren't getting the reply).

    \b
    Examples:
      spec team watch
      spec team watch --compact
      spec team watch --no-heartbeat
      spec team watch --no-critic   # silence rule-based suggestions
      spec team request-push jc --message "need your branch for rebase"
    """
    creds = load_credentials()
    if not creds or not creds.access_token:
        fatal("Not signed in. Run `spec login` first.")
        return

    configure_streaming_stdio()

    assistant_quiet_resolved = _resolve_assistant_quiet_secs(assistant_quiet_secs)

    bundle_watch_root: Path | None = None
    try:
        bundle_watch_root = find_bundle_root()
    except BundleNotFoundError:
        bundle_watch_root = None

    team_watch_self_id: int | None = None
    try:
        _me_client = CloudClient(creds)
        me_raw = _me_client._request("GET", "/api/auth/me")  # noqa: SLF001
        if isinstance(me_raw, dict) and isinstance(me_raw.get("id"), int):
            team_watch_self_id = int(me_raw["id"])
    except Exception:  # noqa: BLE001
        team_watch_self_id = None

    team_local_bid: str | None = None
    if bundle_watch_root is not None:
        try:
            team_local_bid = load_or_create_broadcast_client_id(bundle_watch_root)
        except OSError:
            team_local_bid = None

    # Bounded in-memory event memory shared with the command layer:
    # /summarize, /replay, /status all read from this. Updated in
    # the consumer callback, so command handlers see exactly what
    # has been received in this session. The notifier reads the same
    # deque to attach ``⤷ prompt`` when the REST warm-up skipped the
    # USER row before an assistant reply.
    event_buffer = make_buffer()
    notifier = Notifier(
        compact=compact,
        critic_enabled=critic_enabled,
        notify=notify,
        pairing_buffer=event_buffer,
        viewer_handle=creds.user_handle,
        show_tool_runs=show_tool_runs,
        # Default behavior: strip code blocks from assistant prose so
        # ``spec team watch`` reads as "full AI output without code".
        # ``--show-tool-runs`` enables the structured tool list AND
        # keeps the raw code blocks (reviewers asking for the latter
        # presumably want the former too).
        strip_code_blocks=not show_tool_runs,
        # Non-compact digest: user/error bodies can use the schema cap.
        # Assistant rows that hit ``Notifier.show`` (rare edge cases) use a
        # ~400-char preview; merged ``show_completed_pair`` at turn flush
        # uses the full stored body (same ceiling as ``spec watch``).
        # ``--show-tool-runs`` removes the digest cap everywhere.
        review_feed_full_bodies=not compact,
        assistant_live_cap=(
            None if (compact or show_tool_runs) else 400
        ),
        self_user_id=team_watch_self_id,
        local_broadcast_client_id=team_local_bid,
    )
    stop_event = threading.Event()
    # Tracks the timestamp of the last *visible* output so the idle
    # heartbeat printer doesn't fire on top of fresh content.
    last_output_at = [time.monotonic()]
    qa = _TeamWatchQAState()
    try:
        qa.pair_cloud = CloudClient(creds)
    except Exception:  # noqa: BLE001
        qa.pair_cloud = None

    watch_state = WatchState(critic_enabled=critic_enabled)

    # Map event_id → project_id so /flag can post against the right
    # project when the workspace stream covers multiple bundles. We
    # populate this from the in-memory buffer; older events that
    # have aged out of the buffer are not flaggable via /flag (a
    # reviewer can always fall back to `spec team flag` outside the
    # pane).
    event_to_project: dict[int, int] = {}

    flag_client: CloudClient | None = None
    if commands_enabled:
        try:
            flag_client = CloudClient(creds)
        except Exception:  # noqa: BLE001
            flag_client = None

    def _qa_pair_now() -> None:
        """Slash ``/pair`` — force the merged user+assistant block."""
        if qa.pending_user is None:
            notifier.show_command_result(
                "nothing pending to pair.",
                kind="info",
            )
            return
        if not qa.combined_assistant_chunks():
            notifier.show_command_result(
                "no assistant rows in the Cloud tail for this prompt yet — "
                "the model may still be streaming, `spec watch` may not have "
                "posted assistant rows for this bundle, or assistant "
                "`session_id` may not match the pending user row.",
                kind="info",
            )
            return
        if qa.flush_pair(notifier):
            last_output_at[0] = time.monotonic()
            notifier.show_command_result(
                "paired reply printed (forced).",
                kind="ok",
            )

    recv_brief = (
        f"receiver: SSE assistant prose "
        f"{'on (full bodies when stored)' if verbose else 'OFF — use default verbose; bodies stripped on wire'} · "
        f"layout {'compact' if compact else 'default'} · "
        f"tools+fenced code {'on' if show_tool_runs else 'off'}"
    )
    cmd_ctx = CommandContext(
        notifier=notifier,
        state=watch_state,
        buffer=event_buffer,
        flag_client=flag_client,
        project_for_event=event_to_project.get,
        qa_pair_now=_qa_pair_now,
        cloud_client=flag_client,
        team_watch_receiver_brief=recv_brief,
        bundle_root=bundle_watch_root,
    )

    _live_ev_dedup = LivePromptEventDeduper()

    def _on_connect() -> None:
        # First successful handshake — print the "connected" banner
        # only now, so auth failures stay silent on stdout and the
        # user sees the real error from the SSE consumer instead.
        notifier.announce_connected(WORKSPACE_FEED_LABEL)
        if commands_enabled:
            notifier.show_command_result(
                "interactive commands enabled — type /help for the list. "
                "Use /pair to print the merged Q/A block from Cloud + SSE "
                "(even if intermediate live frames were missed). "
                "From a bundle cwd, /push@handle records a git-push handoff "
                "in `.spec/team-push-requests.yaml`. "
                "After each paired reply, `/turn` (last turn) and `/full` "
                "(whole session) re-fetch full bodies from Cloud using the "
                "session chip from the footer. "
                "Two-stage Ctrl+C still exits.",
                kind="info",
            )
        last_output_at[0] = time.monotonic()

    consumer = SSEConsumer(
        creds.api_base,
        creds.access_token,
        None,
        workspace=True,
        include_presence=include_presence,
        verbose=verbose,
        on_connect=_on_connect,
    )

    def _deliver(
        ev: IncomingEvent,
        *,
        tick_clock: bool = True,
        is_bootstrap: bool = False,
    ) -> None:
        """Shared path for live SSE frames and the one-shot REST warm."""
        if _live_ev_dedup.is_redelivery(ev.id):
            return
        use_qa_coalesce = tick_clock
        event_buffer.append(ev)
        # Workspace-only events intentionally have no project and decode as
        # project_id=0 in the dependency-free CLI wire model. They can be
        # reviewed in the activity pane but cannot use project-scoped /flag.
        if ev.project_id > 0:
            event_to_project[ev.id] = ev.project_id
        if not include_presence and ev.role == "presence":
            return
        # Update the user → AI pairing tracker *before* any visibility
        # / tool-only filter. Otherwise a teammate's tool-only assistant
        # reply (synthesized "ran N tools: …" summary, no critic hit)
        # gets filtered out below and the matching user prompt sits in
        # ``_open_sessions`` forever — firing a bogus 90s no-reply hint
        # even though the AI did reply.
        notifier.record_pairing(ev)
        if not watch_state.is_visible(ev):
            return
        notifier.set_critic_enabled(watch_state.critic_enabled)
        # Tool-only assistant frames are turns the broadcaster could
        # only synthesize a ``ran N tools: …`` summary for (no prose
        # captured upstream). The default ``team watch`` view skips
        # them — most modern adapters now ship the full prose with
        # ``tool_calls`` as structured sidecar, so a tool-only frame
        # usually means an older client or a session whose prose is
        # still streaming. The auto-critic still scans them; if it
        # fires the row surfaces anyway.
        is_prose_assistant = (
            ev.role == "assistant" and _assistant_has_reviewable_prose(ev)
        )
        force_show_assistant = False
        if (
            ev.role == "assistant"
            and not show_tool_runs
            and not is_prose_assistant
            and is_tool_only_summary(ev.summary)
        ):
            critiques = (
                critique_event(ev) if watch_state.critic_enabled else []
            )
            if not critiques:
                # Default view skips drawing these alone, but silently
                # dropping them breaks Q/A coalescing: ``pending_user`` is
                # already set and ``/pair`` would see zero assistant chunks.
                if use_qa_coalesce and qa.buffer_assistant(ev):
                    if tick_clock:
                        last_output_at[0] = time.monotonic()
                    return
                return
            force_show_assistant = True

        # Never render this wire sentinel; flush only on the live SSE path.
        if ev.role == "assistant_closed":
            if use_qa_coalesce:
                qa.flush_on_assistant_closed(ev, notifier, last_output_at)
            return

        if use_qa_coalesce and ev.role == "user":
            qa.on_user(
                ev, notifier, last_output_at, is_bootstrap=is_bootstrap
            )
            return

        if (
            use_qa_coalesce
            and ev.role == "assistant"
            and qa.pending_user is None
            and qa.pair_cloud is not None
        ):
            backfill = _user_from_rest_before_assistant(qa.pair_cloud, ev)
            if backfill is not None:
                qa.on_user(
                    backfill,
                    notifier,
                    last_output_at,
                    is_bootstrap=is_bootstrap,
                )

        if use_qa_coalesce and ev.role == "error":
            qa.flush_on_error(notifier, last_output_at)
            notifier.show(ev)
            if tick_clock:
                last_output_at[0] = time.monotonic()
            return

        if (
            use_qa_coalesce
            and ev.role == "assistant"
            and not force_show_assistant
            and qa.buffer_assistant(ev)
        ):
            # Live preview: show each prose snapshot while we still merge
            # the durable paired block at flush (``assistant_closed``,
            # idle timer, next user, ``/pair``, …).
            if is_prose_assistant:
                notifier.show(ev)
            if tick_clock:
                last_output_at[0] = time.monotonic()
            return
        if (
            use_qa_coalesce
            and ev.role == "assistant"
            and not force_show_assistant
            and qa.should_suppress_lagging_assistant(ev)
        ):
            if tick_clock:
                last_output_at[0] = time.monotonic()
            return

        notifier.show(ev)
        if tick_clock:
            last_output_at[0] = time.monotonic()

    try:
        hist_client = CloudClient(creds)
        boot_events = _build_team_watch_bootstrap_events(
            hist_client,
            limit=_TEAM_WATCH_BOOTSTRAP_LIMIT,
            include_presence=include_presence,
        )
        max_boot_id: int | None = None
        for ev in boot_events:
            if max_boot_id is None or ev.id > max_boot_id:
                max_boot_id = ev.id
            _deliver(ev, tick_clock=True, is_bootstrap=True)
        if max_boot_id is not None:
            consumer.set_resume_cursor(max_boot_id)
    except ApiError:
        pass

    def on_fatal(err: SSEStreamError) -> None:
        notifier.announce_fatal(str(err))
        stop_event.set()

    def on_flag(flag: IncomingFlag) -> None:
        notifier.show_flag(flag)
        last_output_at[0] = time.monotonic()

    # SSE consumer only enqueues; the main thread runs ``_deliver`` so
    # REST merges for Q/A pairing never block the socket reader.
    incoming: queue.Queue[tuple[IncomingEvent, bool]] = queue.Queue()

    def on_event(ev: IncomingEvent) -> None:
        incoming.put((ev, True))

    consumer_thread = run_consumer_in_thread(
        consumer, on_event, on_fatal, on_flag=on_flag
    )

    # Background stdin reader: read one line at a time, parse, and
    # dispatch. Daemonised so a hung readline() does not prevent the
    # process from exiting after the two-stage Ctrl+C completes. We
    # deliberately do not draw a pinned input prompt or use
    # ``rich.live`` — keeping the watcher in a normal scrolling pane
    # preserves terminal scrollback, multiplexer integration, and
    # mouse-copy of past events.
    stdin_thread: threading.Thread | None = None
    if commands_enabled and _stdin_is_interactive():
        stdin_thread = threading.Thread(
            target=_stdin_reader,
            args=(cmd_ctx, stop_event),
            name="spec-team-watch-stdin",
            daemon=True,
        )
        stdin_thread.start()

    # Two-stage Ctrl+C: first press asks the consumer to stop; second
    # raises KeyboardInterrupt (default handler) and bails out of any
    # blocking cleanup. Matches the convention in `spec watch`.
    pressed_once = threading.Event()

    def _stop(_signum: int, _frame: object | None) -> None:
        if not pressed_once.is_set():
            pressed_once.set()
            try:
                dim("spec team watch: shutting down… (press Ctrl+C again to force)")
            except Exception:  # noqa: BLE001
                pass
            consumer.stop()
            stop_event.set()
            try:
                signal.signal(signal.SIGINT, signal.default_int_handler)
            except (AttributeError, ValueError):
                pass

    signal.signal(signal.SIGINT, _stop)
    try:
        signal.signal(signal.SIGTERM, _stop)
    except (AttributeError, ValueError):
        pass

    notifier.announce_connecting("live prompt stream…")

    try:
        while consumer_thread.is_alive() and not stop_event.is_set():
            drained = False
            while True:
                try:
                    ev, tick_clock = incoming.get_nowait()
                except queue.Empty:
                    break
                drained = True
                _deliver(ev, tick_clock=tick_clock)
            qa.tick_quiet_flush(
                notifier,
                last_output_at,
                assistant_quiet_resolved,
            )
            # Surface "AI has not replied" hints on every loop tick.
            # Cheap (O(open_sessions)) and only prints on the first
            # transition past the no-reply threshold per session.
            notifier.check_open_sessions()
            if heartbeat:
                idle_for = time.monotonic() - last_output_at[0]
                if idle_for >= heartbeat_interval:
                    notifier.announce_heartbeat()
                    last_output_at[0] = time.monotonic()
            # After a burst, sleep briefly so we do not busy-spin the CPU
            # while still draining faster than the 1s idle cadence.
            stop_event.wait(timeout=0.05 if drained else 1.0)
    finally:
        consumer.stop()
        consumer_thread.join(timeout=2.0)
        while True:
            try:
                ev, tick_clock = incoming.get_nowait()
            except queue.Empty:
                break
            _deliver(ev, tick_clock=tick_clock)
        qa.flush_shutdown(notifier)


@team_group.command("flag")
@click.argument("event_id", type=int)
@click.option(
    "--kind",
    "-k",
    type=click.Choice(list(_FLAG_KINDS)),
    default="warning",
    show_default=True,
    help="Flag kind (warning · question · block · ack).",
)
@click.option(
    "--note",
    "-m",
    default=None,
    help="Optional short note (max 500 chars).",
)
@click.option(
    "--project",
    "-p",
    default=None,
    help=(
        "Project `handle/slug`. Defaults to `cloud.project` from "
        "spec.yaml when run inside a bundle. Required outside a bundle."
    ),
)
def team_flag_cmd(
    event_id: int, kind: str, note: str | None, project: str | None
) -> None:
    """Flag a teammate's prompt event in near-real-time.

    The flag is delivered to every connected ``spec watch`` /
    ``spec team watch`` over SSE on the ``flag`` channel, so peers
    see it next to the prompt within an RTT. Idempotent: posting the
    same kind for the same event twice yields 409.

    \b
    Examples:
      spec team flag 4711 --kind warning --note "race condition risk"
      spec team flag 4712 --kind ack
      spec team flag 4713 --kind block --note "do not run this"
    """
    creds = load_credentials()
    if not creds or not creds.access_token:
        fatal("Not signed in. Run `spec login` first.")
        return

    raw = project
    if not raw:
        try:
            root = find_bundle_root()
        except BundleNotFoundError:
            fatal(
                "No project specified. Pass `--project <handle>/<slug>` or "
                "run `spec team flag` from inside a Spec bundle."
            )
            return
        manifest = load_manifest(root)
        raw = manifest.cloud_project
        if not raw:
            fatal(
                "No `cloud.project` in spec.yaml. Pass --project <handle>/<slug>."
            )
            return

    try:
        handle, slug = parse_cloud_project(raw, default_handle=creds.user_handle)
    except RemoteUrlError as e:
        fatal(str(e))
        return

    client = CloudClient(creds)
    try:
        project_info = client.resolve_project(handle, slug)
    except ApiError as e:
        fatal(str(e))
        return
    project_id = int(project_info["id"])
    try:
        out = client.create_prompt_event_flag(
            project_id=project_id,
            event_id=event_id,
            kind=kind,
            note=note,
        )
    except ApiError as e:
        fatal(str(e))
        return

    flag_id = out.get("id") if isinstance(out, dict) else None
    ok(
        f"flagged #{event_id} as {kind}"
        + (f" (flag id {flag_id})" if flag_id is not None else "")
    )


# Backwards-compatible export name for cli.py
team_cmd = team_group
