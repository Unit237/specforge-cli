"""
The ``spec watch`` orchestrator: producer + consumer.

Two threads on top of a shared ``LiveCursor``:

* **Producer** — every ``poll_interval`` seconds, scans local Cursor /
  Codex / Claude Code / Compress transcripts, finds turns that haven't been
  broadcast yet (per the cursor), redacts them, and POSTs one event per new
  turn. Under ``spec on`` exactly one watcher owns a machine-wide scan and the
  workspace endpoint; direct legacy watches remain project-scoped.

* **Consumer** — holds an SSE connection on
  ``/api/projects/{id}/prompt-stream``. For each event yielded:
    - skip if it's an echo of our own broadcast (same ``user_id``).
    - dispatch to :class:`Notifier` for terminal output.
    - optionally append to the on-disk peer mirror.

Both threads update the cursor and persist it. On shutdown
(``SIGINT``, ``SIGTERM``, or ``stop_event.set()``) we drain in-flight
work and save the cursor before exiting.
"""
from __future__ import annotations

import hashlib
import itertools
import logging
import os
import queue
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Literal

from ..api import ApiError, CloudClient
from ..git import read_git_context
from ..preferences import load_preferences
from ..prompts.schema import Session, Turn
from ..prompts.text_sanitize import (
    is_cursor_redacted_placeholder,
    prose_without_redacted_placeholders,
    unwrap_cursor_user_message,
)
from .events import IncomingEvent
from ..sources import (
    ClaudeCodeError,
    CodexError,
    CompressError,
    CursorError,
    claude_code_store_root,
    codex_transcript_store_available,
    compress_session_store_root,
    cursor_global_storage_db,
    cursor_workspace_storage_root,
    read_claude_code_sessions,
    read_codex_sessions,
    read_compress_sessions,
    read_cursor_sessions,
    redact_text,
)
from ..stage import historical_bundle_paths, record_bundle_path
from ..ui import configure_streaming_stdio, dim, warn
from .broadcast_identity import load_or_create_broadcast_client_id
from .coordination import CoordinationCache, TeamCoordinationMirror
from .events import OutgoingEvent, ToolCallPayload
from .live_event_dedup import LivePromptEventDeduper
from .mirror import PeerMirror
from .notifier import Notifier, WORKSPACE_FEED_LABEL
from .presence import (
    PRESENCE_FRESHNESS_SECS,
    LocalPresence,
    PresenceCache,
    compute_local_presence,
)
from .presence_mirror import TeamPresenceMirror
from .live_doctor import QUIET_PROMPT_POST_SECS, emit_live_doctor_warnings
from .tracker import LiveCursor, PRODUCER_BASELINE_VERSION
from .transport import (
    HTTPPoster,
    SSEConsumer,
    SSEStreamError,
    run_consumer_in_thread,
)

log = logging.getLogger(__name__)


# How long the producer pauses between local-transcript scans. 2s is
# the sweet spot in practice: short enough to feel "live" (worst-case
# 2s + network RTT), long enough that a quiet bundle costs ~nothing.
DEFAULT_POLL_INTERVAL_SECS = 2.0
# A resumed old chat updates its local activity timestamp and re-enters this
# window. Limiting machine scans to active history avoids reparsing years of
# dormant Cursor conversations every two seconds.
MACHINE_SESSION_LOOKBACK = timedelta(days=2)
# Presence broadcasts are far less urgent than prompt turns — git diff
# every 15s is enough for "Alice is in auth.py" to feel instant
# without spamming the wire while a teammate is mid-typing. The cache
# expiry is 5 min so a missed 1-2 ticks here is invisible.
DEFAULT_PRESENCE_INTERVAL_SECS = 15.0
# Heartbeat for the team-presence.json mirror. We only rewrite the
# file when the cache changed; this just bounds how often the
# expiry sweep runs in case no events arrive but a peer aged out.
DEFAULT_TEAM_PRESENCE_TICK_SECS = 30.0
# How often the cursor file is fsync'd to disk during steady-state
# operation. We always save on shutdown, but a periodic save protects
# against a kill -9 / power loss costing more than a few seconds of
# replays.
CURSOR_SAVE_INTERVAL_SECS = 10.0
# Recent Cloud rows printed only when ``spec watch --bootstrap`` is requested.
# A normal join is live-only and does not seed the SSE connection from a
# persisted consumer cursor.
WATCH_BOOTSTRAP_LIMIT_DEFAULT = 80
# Hard cap on per-event text payload before redaction. The server caps
# at 512 KB; we cap a hair below to avoid edge-of-frame rejections,
# leaving room for redaction expanding text by a few bytes.
MAX_TURN_TEXT_CHARS = 480 * 1024
# Below this many chars, drop the turn entirely. A 0-char "user" turn
# is almost always an artefact of an empty draft submit; we don't want
# to flood the team feed with blanks.
MIN_TURN_TEXT_CHARS = 1
# Empty local turns we retry before advancing the broadcast cursor
# (advancing on empty skip without a POST inflated the cursor past the
# real transcript and caused duplicate Cloud rows on shrink).
_MAX_EMPTY_TURN_RETRIES = 8
_EMPTY_TURN_RETRIES: dict[tuple[str, int], int] = {}

# Final assistant turns (Cursor streams into one bubble) may grow on
# disk between producer polls. We POST updates while ``text``
# changes, then delay advancing ``broadcast_turns`` until the body
# stays unchanged long enough that token streams are unlikely to
# resume in the same bubble. A few-second quiet window matches human
# typing pauses between streamed chunks; ``assistant_closed`` must
# arrive promptly so ``spec team watch`` can flush Q/A pairs.
# Override with ``SPEC_LIVE_TAIL_STABILITY_SECS`` (e.g. ``120`` for
# very slow Codex runs between tool rounds).
TAIL_ASSISTANT_STABILITY_FLOOR_SECS = 12.0
TAIL_ASSISTANT_STABILITY_POLL_MULTIPLIER = 3.0


def tail_stability_quiet_secs(
    poll_interval: float, *, tool_count: int = 0
) -> float:
    """How long the tail assistant turn must be unchanged before we
    treat the bubble as finished and POST ``assistant_closed``.

    Override with ``SPEC_LIVE_TAIL_STABILITY_SECS`` (seconds). While
    the model is still writing, the fingerprint keeps changing and we
    keep POSTing updates — this quiet window only applies after the
    last visible chunk. Tool-heavy tails get a longer floor so we do
    not emit ``turn complete`` while JSONL is still accumulating tools.
    """
    raw = os.environ.get("SPEC_LIVE_TAIL_STABILITY_SECS", "").strip()
    if raw:
        try:
            custom = float(raw)
            if custom > 0:
                base = max(
                    custom,
                    poll_interval * TAIL_ASSISTANT_STABILITY_POLL_MULTIPLIER,
                )
                if tool_count > 0:
                    return max(base, 20.0)
                return base
        except ValueError:
            pass
    base = max(
        TAIL_ASSISTANT_STABILITY_FLOOR_SECS,
        poll_interval * TAIL_ASSISTANT_STABILITY_POLL_MULTIPLIER,
    )
    if tool_count > 0:
        return max(base, 20.0)
    return base


@dataclass
class _AssistantTailHold:
    """Tracks the last assistant bubble while it may still be streaming."""

    turn_idx: int
    fp: str
    last_fp_change: float  # ``time.monotonic()`` when ``fp`` last changed


def _assistant_turn_fingerprint(turn: Turn) -> str:
    """Fingerprint tail stability from prose, summary, and tool activity."""
    tool_calls = turn.tool_calls or []
    prose = prose_without_redacted_placeholders((turn.text or "").strip())
    summ = prose_without_redacted_placeholders((turn.summary or "").strip())
    tool_names = ",".join(
        sorted(
            n
            for n in (getattr(c, "name", None) or "" for c in tool_calls)
            if n
        )
    )
    payload = f"{prose}\0{summ}\0{len(tool_calls)}\0{tool_names}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _post_assistant_closed(
    poster: HTTPPoster,
    session: Session,
    *,
    branch: str | None,
    git,
    opts: WatcherOptions,
    last_assistant_cloud_id: int | None,
) -> None:
    """POST ``role=assistant_closed`` so receivers flush coalesced Q/A.

    The optional ``last_assistant_cloud_id`` ties the sentinel to the
    last assistant row returned by ``POST /prompt-events`` for this
    session (when the HTTP response included ``id``).
    """
    src = (
        session.source
        if session.source in (
            "cursor", "codex", "claude_code", "compress", "manual"
        )
        else "manual"
    )
    evt = OutgoingEvent(
        session_id=session.id,
        source=src,
        role="assistant_closed",
        branch=branch or None,
        commit_sha=git.commit_sha if git else None,
        model=session.model,
        phase=None,
        summary=None,
        text=None,
        title=redact_text((session.title or "").strip()) or None,
        cwd=session.cwd,
        paths_touched=list(session.paths_touched or []),
        turn_at=_now_utc(),
        closes_event_id=last_assistant_cloud_id,
        broadcast_client_id=opts.broadcast_client_id,
    )
    # ``HTTPPoster.send`` already retries transient errors; one extra
    # pass here because a dropped sentinel strands teammates' Q/A in
    # ``spec team watch`` until idle timeout.
    for attempt in range(2):
        ok, _ = poster.send(evt)
        if ok:
            return
        if attempt == 0:
            time.sleep(0.35)
    log.warning(
        "spec-live: assistant_closed POST failed for session %s",
        (session.id or "")[:32],
    )


@dataclass
class WatcherOptions:
    """User-controlled toggles for ``spec watch``."""

    project_id: int
    project_label: str
    api_base: str
    access_token: str
    self_user_id: int | None
    self_handle: str | None = None
    self_name: str | None = None
    # Stable per-(machine, bundle) id (``~/.spec/broadcast-client-ids/``).
    # Used when posting and when filtering SSE echoes on ``spec watch``.
    broadcast_client_id: str | None = None
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECS
    presence_interval: float = DEFAULT_PRESENCE_INTERVAL_SECS
    broadcast: bool = True
    # Prompt production is owned by exactly one boundary. ``machine`` is the
    # watcher elected by ``spec on`` and scans every local agent store into
    # the workspace endpoint. Other registered watchers use ``none`` and keep
    # repository presence only. ``project`` preserves direct/legacy watches.
    prompt_scope: Literal["project", "machine", "none"] = "project"
    receive: bool = True
    mirror: bool = False
    presence_enabled: bool = True
    # Broadcaster verbosity: when True (default since v0.4) we POST
    # the full assistant ``text`` body alongside the summary so
    # ``spec team watch`` viewers can debug the AI's actual output.
    # Teams wanting the old "summary-only" posture can flip this off
    # via ``cloud.prompt_stream.verbose: false`` in ``spec.yaml``.
    verbose_assistant: bool = True
    compact_output: bool = False
    # Mirror of the ``spec team watch --show-tool-runs`` toggle for
    # the regular foreground watcher. When True, the notifier expands
    # each incoming assistant turn's structured ``tool_calls`` list
    # under the prose body AND leaves fenced code blocks intact. Off
    # by default so the pane shows prose narration only.
    show_tool_runs: bool = False
    project_branch_filter: str | None = None
    # Opt-in history mode: fetch recent workspace rows and resume from the
    # persisted receive cursor before opening the SSE tail. A normal join
    # starts at the current stream position.
    bootstrap_receive: bool = False
    # A foreground workspace viewer may overlap a background broadcaster for
    # this bundle. In that case the daemon exclusively owns the shared cursor
    # file and the viewer keeps its receive position in memory only.
    persist_cursor: bool = True
    user_agent: str = field(
        default_factory=lambda: "spec-cli/live"
    )
    started_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# Throttle terminal noise when the cloud POST path is failing every tick.
_POST_FAILURE_WARN_MONO: list[float] = [0.0]


def _watch_bootstrap_limit() -> int:
    raw = os.environ.get("SPEC_WATCH_BOOTSTRAP_LIMIT", "").strip()
    if raw.isdigit():
        return max(0, min(int(raw), 200))
    return WATCH_BOOTSTRAP_LIMIT_DEFAULT


def build_watch_bootstrap_events(
    client: CloudClient,
    *,
    limit: int | None = None,
) -> list[IncomingEvent]:
    """Recent visible workspace prompt rows, oldest-first, for startup replay.

    Skips ``presence`` rows (noisy git pings). Fetches extra rows then
    keeps the newest ``limit`` by monotonic ``id``.
    """
    cap = _watch_bootstrap_limit() if limit is None else max(0, min(int(limit), 200))
    if cap == 0:
        return []
    fetch = min(max(cap * 2, cap), 200)
    try:
        rows = client.list_my_prompt_events(limit=fetch)
    except ApiError:
        return []
    by_id: dict[int, IncomingEvent] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        role = raw.get("role")
        if role == "presence":
            continue
        if role == "assistant_closed":
            # Transport sentinel for coalescing, not visible history. Keeping
            # it out of bootstrap prevents long Codex runs from evicting the
            # human prompt and named chat context from the replay window.
            continue
        try:
            ev = IncomingEvent.from_json(raw)
        except (KeyError, TypeError, ValueError):
            continue
        if ev.id >= 0:
            by_id[ev.id] = ev
    ordered = sorted(by_id.values(), key=lambda e: e.id)
    if len(ordered) > cap:
        ordered = ordered[-cap:]
    return ordered


def _spec_live_startup_snapshot(
    bundle_root: Path, *, machine_wide: bool = False
) -> None:
    """One-time human-readable scan of local agent stores.

    When broadcasting is on but every adapter shows zero mapped
    sessions, the team feed stays quiet — this line is the fastest
    way to spot a Cursor workspace-path mismatch or a missing install.
    """
    paths = None if machine_wide else historical_bundle_paths(bundle_root)
    since = (
        datetime.now(timezone.utc) - MACHINE_SESSION_LOOKBACK
        if machine_wide
        else None
    )

    def _visible(sessions: Iterable[Session]) -> Iterable[Session]:
        return sessions if paths is None else _scoped_sessions(sessions, paths)

    bits: list[str] = []
    n_cursor = -1
    n_claude = -1
    n_codex = -1
    n_compress = -1

    ws_root = cursor_workspace_storage_root()
    if not ws_root.exists():
        n_cursor = 0
        bits.append("cursor: no workspaceStorage tree")
    else:
        gdb = cursor_global_storage_db()
        if not gdb.is_file():
            n_cursor = 0
            bits.append("cursor: no global state.vscdb (cannot load bubbles)")
        else:
            try:
                n_cursor = sum(
                    1
                    for _ in itertools.islice(
                        _visible(
                            read_cursor_sessions(paths, since=since, verbose=True)
                        ),
                        400,
                    )
                )
                scope = "on this machine" if machine_wide else "for this bundle"
                bits.append(f"cursor: {n_cursor} composer session(s) {scope}")
            except CursorError as e:
                n_cursor = -2
                bits.append("cursor: read error")
                warn(f"Spec Live — could not read Cursor transcripts: {e}")

    if claude_code_store_root().exists():
        try:
            n_claude = sum(
                1
                for _ in itertools.islice(
                    _visible(
                        read_claude_code_sessions(
                            paths, since=since, verbose=True
                        ),
                    ),
                    400,
                )
            )
            bits.append(f"claude_code: {n_claude} session(s)")
        except ClaudeCodeError as e:
            n_claude = -2
            bits.append(f"claude_code: skip ({e})")
    else:
        n_claude = 0
        bits.append("claude_code: store not found")

    if codex_transcript_store_available():
        try:
            n_codex = sum(
                1
                for _ in itertools.islice(
                    _visible(
                        read_codex_sessions(paths, since=since, verbose=True),
                    ),
                    400,
                )
            )
            bits.append(f"codex: {n_codex} session(s)")
        except CodexError as e:
            n_codex = -2
            bits.append(f"codex: skip ({e})")
    else:
        n_codex = 0
        bits.append("codex: not installed")

    if compress_session_store_root().exists():
        try:
            n_compress = sum(
                1
                for _ in itertools.islice(
                    _visible(
                        read_compress_sessions(paths, since=since, verbose=True),
                    ),
                    400,
                )
            )
            bits.append(f"compress: {n_compress} session(s)")
        except CompressError as e:
            n_compress = -2
            bits.append(f"compress: skip ({e})")
    else:
        n_compress = 0
        bits.append("compress: store not found")

    dim("Spec Live broadcast · local scan — " + " · ".join(bits))
    if machine_wide:
        dim("Spec Live machine broadcaster · every supported local agent session is scanned.")
    else:
        dim(
            "Tip: only Cursor Composer / in-editor Agent sessions in this folder "
            "are scanned (workspaceStorage). The sidebar Chat panel often lives "
            "elsewhere — run `spec on` for machine-wide broadcasting."
        )
    if (
        n_cursor >= 0
        and n_claude >= 0
        and n_codex >= 0
        and n_compress >= 0
        and (n_cursor + n_claude + n_codex + n_compress) == 0
    ):
        warn(
            "Spec Live: no Cursor / Claude Code / Codex / Compress sessions are mapped to "
            "this bundle path yet — open this repo (or its parent workspace) in "
            "your agent IDE, start a thread, then restart `spec watch`."
        )


def run_watcher(
    bundle_root: Path,
    opts: WatcherOptions,
    *,
    stop_event: threading.Event | None = None,
) -> int:
    """Run the watcher to completion (until SIGINT / SIGTERM).

    Returns an exit code suitable for ``sys.exit``: 0 on graceful
    shutdown, 1 on a fatal stream error (auth, project resolution).

    ``stop_event`` is an optional externally-owned ``threading.Event``
    the caller can set to request a graceful shutdown without going
    through SIGINT. Used by tests (so we don't have to send real
    signals to the test runner) and by embedding code that runs the
    watcher inside another process and wants its own stop knob.
    The internal SIGINT/SIGTERM handlers always set this same event,
    so external callers and signal handlers compose cleanly.
    """
    configure_streaming_stdio()
    record_bundle_path(bundle_root)
    cursor = LiveCursor.load(bundle_root, project_id=opts.project_id)
    cursor.project_id = opts.project_id

    if opts.broadcast_client_id is None:
        opts.broadcast_client_id = load_or_create_broadcast_client_id(
            bundle_root
        )

    produces_prompts = opts.broadcast and opts.prompt_scope != "none"
    machine_wide = opts.prompt_scope == "machine"
    baselined = (
        _establish_live_baseline(
            cursor, bundle_root, machine_wide=machine_wide
        )
        if produces_prompts
        else 0
    )
    if baselined:
        dim(
            "Spec Live baseline · "
            f"skipped {baselined} existing local session(s); watching new turns"
        )

    notifier = Notifier(
        compact=opts.compact_output,
        show_tool_runs=opts.show_tool_runs,
        # When tool-runs view is on, leave fenced code blocks in
        # prose intact too — a reviewer asking for tool detail wants
        # the code itself. When off, strip code blocks so the default
        # pane shows prose narration without dumps of pasted code.
        strip_code_blocks=not opts.show_tool_runs,
        self_user_id=opts.self_user_id,
        local_broadcast_client_id=opts.broadcast_client_id,
    )
    if not opts.broadcast:
        notifier.announce_broadcast_disabled()
    else:
        if produces_prompts:
            _spec_live_startup_snapshot(
                bundle_root, machine_wide=machine_wide
            )
        if not machine_wide:
            emit_live_doctor_warnings(bundle_root, watcher_running_here=True)

    if stop_event is None:
        stop_event = threading.Event()
    fatal_error: list[SSEStreamError] = []

    # Two-stage Ctrl+C convention:
    #   1st SIGINT → request graceful shutdown (set ``stop_event``,
    #                tell the user we're winding down, restore the
    #                default SIGINT handler).
    #   2nd SIGINT → default handler raises ``KeyboardInterrupt`` and
    #                aborts whatever cleanup step is still running.
    # Without the second stage, a slow shutdown step (a hung POST, a
    # network partition during the final clean-state broadcast) would
    # leave the user with no escape hatch — pressing Ctrl+C again
    # would silently re-enter ``_request_stop`` and do nothing.
    is_main_thread = threading.current_thread() is threading.main_thread()
    original_sigint = signal.getsignal(signal.SIGINT) if is_main_thread else None
    original_sigterm = (
        signal.getsignal(signal.SIGTERM) if is_main_thread else None
    )

    def _request_stop(*_a, **_kw) -> None:
        if not stop_event.is_set():
            stop_event.set()
            try:
                # Stderr so the message appears even if stdout is
                # buffered (Rich, pipe to file, etc.). Best-effort:
                # if even the dim-write fails, we just keep going.
                dim(
                    "spec watch: shutting down… (press Ctrl+C again to force quit)"
                )
            except Exception:  # noqa: BLE001
                pass
            # Restore the platform default so a second Ctrl+C aborts
            # any blocking cleanup step. This is the standard "press
            # twice to escape" UX for long-running daemons.
            if is_main_thread:
                try:
                    signal.signal(signal.SIGINT, signal.default_int_handler)
                except (AttributeError, ValueError):
                    pass
                try:
                    signal.signal(signal.SIGTERM, signal.default_int_handler)
                except (AttributeError, ValueError):
                    pass

    # We can only install signal handlers on the main thread; if the
    # watcher is ever invoked from a worker thread (tests, embedding),
    # the handlers are skipped — the ``stop_event`` is still the
    # authoritative knob.
    if is_main_thread:
        signal.signal(signal.SIGINT, _request_stop)
        try:
            signal.signal(signal.SIGTERM, _request_stop)
        except (AttributeError, ValueError):
            pass

    poster: HTTPPoster | None = None
    workspace_poster: HTTPPoster | None = None
    consumer: SSEConsumer | None = None
    consumer_thread: threading.Thread | None = None
    mirror = PeerMirror(bundle_root) if opts.mirror else None

    # File-level presence cache + on-disk mirror. Always wired up
    # (receiving doesn't cost anything and the mirror is what AI
    # tools read), but broadcasting only fires when both
    # ``presence_enabled`` and ``broadcast`` are true so the user's
    # opt-out flags reach this path.
    presence_cache = PresenceCache(freshness_secs=PRESENCE_FRESHNESS_SECS)
    team_presence = TeamPresenceMirror(bundle_root)
    coordination_cache = CoordinationCache(bundle_root)
    team_coordination = TeamCoordinationMirror(bundle_root)
    last_local_presence: list[LocalPresence] = []  # nonlocal-able mutable handle
    # Clear a stale projection left by an interrupted prior watcher before
    # bootstrap replay has a chance to rebuild it from Cloud.
    team_coordination.sync(coordination_cache)

    if opts.broadcast:
        poster = HTTPPoster(
            opts.api_base, opts.access_token, opts.project_id, user_agent=opts.user_agent
        )
        workspace_poster = HTTPPoster(
            opts.api_base,
            opts.access_token,
            None,
            workspace=True,
            user_agent=opts.user_agent,
        )

    if opts.receive:
        consumer = SSEConsumer(
            opts.api_base,
            opts.access_token,
            workspace=True,
            include_presence=True,
            verbose=opts.verbose_assistant,
            user_agent=opts.user_agent,
        )
        _live_ev_dedup = LivePromptEventDeduper()
        # SSE reader thread only enqueues — the main loop drains and
        # renders. Blocking Rich I/O on the reader used to stall
        # ``iter_lines``, fill the server hub queue, and drop live
        # frames teammates had already POSTed.
        incoming: queue.Queue = queue.Queue()

        def _process_incoming_event(event) -> None:  # type: ignore[no-untyped-def]
            try:
                if _live_ev_dedup.is_redelivery(event.id):
                    return
                # Materialize and render every visible prompt event, including
                # this install's own echo. A solo user's workspace feed is
                # still useful, and suppressing that echo made ``spec watch``
                # look empty when nobody else was on the account.
                if coordination_cache.apply_event(event):
                    team_coordination.sync(coordination_cache)
                if event.role == "presence":
                    # Conversation turns are workspace-wide, but dirty-file
                    # presence is a repository safety boundary. Never project
                    # another bundle's ``auth.py`` into this bundle's mirror.
                    if event.project_id != opts.project_id:
                        return
                    # Presence updates land in the cache (and trigger a
                    # mirror rewrite below); we do NOT print them to the
                    # terminal — the dirty file list churns too much for
                    # a scrolling log to be useful, and the
                    # ``team-presence.json`` writer is the canonical
                    # surface. We'll surface a one-line "alice is now
                    # editing X" only on transitions; that's a §future
                    # polish.
                    if presence_cache.apply_event(event):
                        _write_team_presence(
                            bundle_root,
                            opts,
                            presence_cache,
                            team_presence,
                            last_local_presence,
                        )
                    return
                notifier.show(event)
                if mirror is not None:
                    mirror.write_event(event)
            finally:
                # Advance the resume cursor only after we have accepted
                # the frame locally. Updating before render meant a
                # crash or slow terminal could skip rows on reconnect.
                cursor.record_received(event.id)

        def _drain_incoming() -> None:
            while True:
                try:
                    event = incoming.get_nowait()
                except queue.Empty:
                    break
                try:
                    _process_incoming_event(event)
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "spec-live: incoming handler raised on event %s: %s",
                        getattr(event, "id", "?"),
                        e,
                    )

        def _on_event(event) -> None:  # type: ignore[no-untyped-def]
            incoming.put(event)

        def _on_fatal(err: SSEStreamError) -> None:
            fatal_error.append(err)
            notifier.announce_fatal(str(err))
            stop_event.set()

        if opts.bootstrap_receive:
            try:
                hist = CloudClient(
                    api_base=opts.api_base, access_token=opts.access_token
                )
                boot = build_watch_bootstrap_events(hist)
            except Exception:  # noqa: BLE001
                boot = []
            if boot:
                for ev in boot:
                    _process_incoming_event(ev)
                dim(
                    f"spec watch: replayed {len(boot)} recent event(s) from Cloud "
                    f"(then live tail)."
                )
            resume = cursor.last_received_id
            if resume is not None:
                consumer.set_resume_cursor(resume)

        notifier.announce_connected(WORKSPACE_FEED_LABEL)

        consumer_thread = run_consumer_in_thread(
            consumer, on_event=_on_event, on_fatal=_on_fatal
        )

    last_save = time.monotonic()
    last_presence_broadcast = 0.0
    assistant_tail_holds: dict[str, _AssistantTailHold] = {}
    last_assistant_cloud_ids: dict[str, int] = {}
    # First loop iteration should run the team-presence mirror tick so
    # hooks see a fresh file quickly; peers still expire on schedule.
    last_team_presence_tick = time.monotonic() - DEFAULT_TEAM_PRESENCE_TICK_SECS
    last_presence_fingerprint = ""
    last_prompt_post_mono = time.monotonic()
    last_doctor_mono = 0.0
    # Prime the mirror with one local snapshot so ``self`` (branch +
    # dirty files) exists before the first 15s presence POST tick.
    try:
        last_local_presence[:] = [compute_local_presence(bundle_root)]
    except Exception:  # noqa: BLE001
        pass
    _write_team_presence(
        bundle_root,
        opts,
        presence_cache,
        team_presence,
        last_local_presence,
    )
    try:
        while not stop_event.is_set():
            if opts.receive:
                _drain_incoming()
            tick_started = time.monotonic()
            if poster is not None and produces_prompts:
                try:
                    posted = _producer_tick(
                        bundle_root=bundle_root,
                        cursor=cursor,
                        poster=poster,
                        workspace_poster=workspace_poster,
                        opts=opts,
                        stop_event=stop_event,
                        assistant_tail_holds=assistant_tail_holds,
                        last_assistant_cloud_ids=last_assistant_cloud_ids,
                        machine_wide=machine_wide,
                    )
                    if posted > 0:
                        last_prompt_post_mono = time.monotonic()
                except Exception as e:  # noqa: BLE001
                    log.warning("spec-live: producer tick error: %s", e)

            now = time.monotonic()

            if (
                produces_prompts
                and poster is not None
                and not machine_wide
                and now - last_prompt_post_mono >= QUIET_PROMPT_POST_SECS
                and now - last_doctor_mono >= QUIET_PROMPT_POST_SECS
            ):
                emit_live_doctor_warnings(bundle_root, watcher_running_here=True)
                last_doctor_mono = now

            # Presence broadcast — gated by `presence_enabled` so a
            # user who muted Spec Live entirely doesn't ship presence
            # either; gated by `broadcast` so a `--no-broadcast`
            # invocation still populates the local cache + mirror
            # without spraying outward.
            if (
                opts.presence_enabled
                and poster is not None
                and now - last_presence_broadcast >= opts.presence_interval
            ):
                try:
                    local = compute_local_presence(bundle_root)
                    last_local_presence[:] = [local]
                    if local.fingerprint != last_presence_fingerprint:
                        if _broadcast_presence(local, poster, opts, bundle_root):
                            last_presence_fingerprint = local.fingerprint
                        # Always rewrite the mirror — the local user's
                        # snapshot has changed even if the broadcast
                        # didn't reach the server yet.
                        _write_team_presence(
                            bundle_root,
                            opts,
                            presence_cache,
                            team_presence,
                            last_local_presence,
                        )
                except Exception as e:  # noqa: BLE001
                    log.warning("spec-live: presence tick error: %s", e)
                last_presence_broadcast = now

            # Periodic mirror tick — picks up cache expiry even if no
            # new presence events arrive (so a peer who closed their
            # laptop disappears from the file in bounded time).
            if now - last_team_presence_tick >= DEFAULT_TEAM_PRESENCE_TICK_SECS:
                _write_team_presence(
                    bundle_root,
                    opts,
                    presence_cache,
                    team_presence,
                    last_local_presence,
                )
                team_coordination.sync(coordination_cache)
                last_team_presence_tick = now

            if (
                opts.persist_cursor
                and now - last_save >= CURSOR_SAVE_INTERVAL_SECS
            ):
                cursor.save()
                last_save = now

            elapsed = time.monotonic() - tick_started
            sleep_for = max(0.1, opts.poll_interval - elapsed)
            stop_event.wait(timeout=sleep_for)
    finally:
        # Tear down in an order that maximises responsiveness to a
        # second Ctrl+C: stop the consumer FIRST (so the SSE socket
        # closes and the receiver thread exits), then do best-effort
        # network cleanup, then save the cursor. The consumer thread
        # is a daemon, so even if join() times out we won't block
        # process exit.
        if consumer is not None:
            consumer.stop()
        if consumer_thread is not None:
            consumer_thread.join(timeout=1.5)
        if opts.receive:
            _drain_incoming()

        # Best-effort: broadcast a clean-state event before stopping
        # so peers can drop our presence row immediately instead of
        # waiting for the freshness window. We use a tight 3s timeout
        # here (vs. the default 15s) so a slow / dead network can't
        # stall the daemon's exit. If we miss the broadcast, peers'
        # caches still expire after the freshness window — annoying
        # but correct.
        if opts.presence_enabled and poster is not None:
            try:
                empty = LocalPresence(
                    files=[],
                    head_commit=None,
                    fingerprint="",
                )
                _broadcast_presence(
                    empty,
                    poster,
                    opts,
                    bundle_root,
                    force_clean=True,
                    timeout=3.0,
                )
            except Exception:  # noqa: BLE001
                pass
        if opts.persist_cursor:
            cursor.save()
        if poster is not None:
            poster.close()
        if workspace_poster is not None:
            workspace_poster.close()
        # Restore the signal handlers we replaced so a host process
        # (tests, embedding) sees its original behaviour after the
        # watcher returns.
        if is_main_thread:
            try:
                signal.signal(signal.SIGINT, original_sigint or signal.default_int_handler)
            except (AttributeError, ValueError, TypeError):
                pass
            try:
                if original_sigterm is not None:
                    signal.signal(signal.SIGTERM, original_sigterm)
            except (AttributeError, ValueError, TypeError):
                pass
        dim("spec watch: stopped")

    return 1 if fatal_error else 0


def _write_team_presence(
    bundle_root: Path,
    opts: WatcherOptions,
    cache: PresenceCache,
    team_presence: TeamPresenceMirror,
    last_local: list[LocalPresence],
) -> None:
    """Refresh ``.spec/team-presence.json`` from the cache + last
    known local snapshot. Quiet on the happy path; logs at debug level
    on every write so a curious user can ``--verbose`` to see when
    the mirror is updating."""
    try:
        local = last_local[0] if last_local else None
        # Always derive ``self.branch`` from this machine's git — never
        # from inbound SSE (that would be a peer's branch name).
        branch = read_git_context(bundle_root).branch
        team_presence.write(
            cache,
            local=local,
            self_handle=opts.self_handle,
            self_name=opts.self_name,
            branch=branch,
        )
    except Exception as e:  # noqa: BLE001
        log.debug("spec-live: team-presence write skipped: %s", e)


def _broadcast_presence(
    local: LocalPresence,
    poster: HTTPPoster,
    opts: WatcherOptions,
    bundle_root: Path,
    *,
    force_clean: bool = False,
    timeout: float | None = None,
) -> bool:
    """Build and send one presence event. Returns True on success.

    ``force_clean`` is the shutdown path: even when the local
    snapshot is empty (no diffs), the watcher wants to broadcast
    one final clean-state event so peers' caches drop the row
    immediately. The producer-tick path skips clean-state broadcasts
    when the previous broadcast was already clean (no fingerprint
    change → no work).

    ``timeout`` overrides the default POST timeout for this call —
    the shutdown path uses a tight value so a slow network doesn't
    stall the daemon's exit.
    """
    payload = local.to_payload()
    if force_clean:
        payload.is_clean = True

    git = read_git_context(bundle_root)
    branch = git.branch or None

    file_count = len(payload.files)
    total_lines = sum(f.lines_added + f.lines_removed for f in payload.files)
    if payload.is_clean and not payload.files:
        summary = "working tree clean"
    elif file_count == 1:
        f = payload.files[0]
        summary = f"{f.path} (+{f.lines_added}/-{f.lines_removed})"
    else:
        summary = f"{file_count} files, +{total_lines} lines"

    event = OutgoingEvent(
        # Stable session id so the server-side dedupe (session_id +
        # role + turn_at) doesn't pile up redundant rows for the
        # same user across the day.
        session_id=f"presence:{opts.project_id}",
        source="git",
        role="presence",
        branch=branch,
        commit_sha=payload.head_commit,
        summary=summary,
        text=None,
        title=None,
        cwd=str(bundle_root),
        paths_touched=[f.path for f in payload.files][:64],
        presence=payload,
        turn_at=datetime.now(timezone.utc),
        broadcast_client_id=opts.broadcast_client_id,
    )
    return poster.send(event, timeout=timeout)[0]


# ── producer logic ─────────────────────────────────────────────────


def _producer_tick(
    *,
    bundle_root: Path,
    cursor: LiveCursor,
    poster: HTTPPoster,
    workspace_poster: HTTPPoster | None = None,
    opts: WatcherOptions,
    stop_event: threading.Event,
    assistant_tail_holds: dict[str, _AssistantTailHold] | None = None,
    last_assistant_cloud_ids: dict[str, int] | None = None,
    machine_wide: bool = False,
) -> int:
    """One pass over local transcripts; broadcast new turns.

    Returns the number of prompt turns successfully POSTed this tick.

    Sessions come from Cursor, Claude Code, Codex, and Compress adapters in
    ``_iter_local_sessions`` — tail-assistant streaming and empty-tail
    retry rules apply to every ``session.source``; there is no
    source-specific branch in the POST path.

    Quiet on the happy path. Errors at the per-source level are
    swallowed so a transient SQLite lock on Cursor's store doesn't
    take down the whole watcher.

    Honours ``stop_event`` between every session and every turn so a
    Ctrl+C can break out within a single network RTT, even when the
    tick is mid-way through a backlog of dozens of turns. Each
    ``poster.send`` is a 15s-timeout HTTP call; without these checks
    a fresh ``spec watch`` against a long-quiet bundle could spend
    minutes ignoring the user's Ctrl+C.
    """
    posted_count = 0
    if stop_event.is_set():
        return posted_count
    git = None if machine_wide else read_git_context(bundle_root)
    branch = None if machine_wide else git.branch or "detached"
    if (
        not machine_wide
        and
        opts.project_branch_filter
        and opts.project_branch_filter != branch
    ):
        return posted_count  # outside the filter — skip this tick entirely
    paths = historical_bundle_paths(bundle_root)
    holds = assistant_tail_holds if assistant_tail_holds is not None else {}
    cloud_ids = (
        last_assistant_cloud_ids if last_assistant_cloud_ids is not None else {}
    )

    session_scope = None if machine_wide else paths
    session_since = (
        opts.started_at - MACHINE_SESSION_LOOKBACK if machine_wide else None
    )
    sessions = (
        _iter_local_sessions(session_scope, since=session_since)
        if session_since is not None
        else _iter_local_sessions(session_scope)
    )
    for session in sessions:
        if stop_event.is_set():
            return posted_count
        route = "workspace" if machine_wide else _session_route(session, paths)
        session_poster = workspace_poster if route == "workspace" else poster
        if session_poster is None:
            continue
        event_branch = None if route == "workspace" else branch
        event_git = None if route == "workspace" else git
        if (
            cursor.producer_baseline_version >= PRODUCER_BASELINE_VERSION
            and not cursor.has_session(session.id)
        ):
            first_live_turn = next(
                (
                    index
                    for index, turn in enumerate(session.turns)
                    if turn.at is not None
                    and _as_utc(turn.at) >= opts.started_at
                ),
                None,
            )
            if first_live_turn is None:
                cursor.record_broadcast(session.id, len(session.turns))
                continue
            cursor.record_broadcast(session.id, first_live_turn)
        prev = cursor.turns_broadcast_for(session.id)
        # Cursor (and other adapters) can shrink on-disk turn lists while
        # our cursor still counts empty skips we advanced past without a
        # successful POST. Clamp to the local length — do *not* rewind to
        # zero and re-POST history every poll (that spammed Cloud and made
        # ``spec team watch`` look like an endless replay loop).
        if prev > len(session.turns):
            clamped = len(session.turns)
            log.info(
                "spec-live: clamping broadcast cursor for session %s "
                "(%s → %s local turns)",
                session.id[:8],
                prev,
                clamped,
            )
            cursor.prune_posted_keys_from_index(session.id, clamped)
            cursor.clamp_broadcast(session.id, clamped)
            prev = clamped
        new_turns = session.turns[prev:]
        if not new_turns:
            continue

        for offset, turn in enumerate(new_turns):
            if stop_event.is_set():
                return posted_count
            event = _build_outgoing(
                session,
                turn,
                branch=event_branch,
                git=event_git,
                opts=opts,
            )
            if event is None:
                # Retry empty / undeliverable slots — do not advance the
                # broadcast cursor until we POST or exhaust retries.
                # Advancing without a POST let ``broadcast_turns`` overshoot
                # ``len(session.turns)`` and later rewinds/clamps spammed
                # duplicate user rows to Cloud.
                turn_idx_empty = prev + offset
                if (
                    turn.role == "assistant"
                    and turn_idx_empty == len(session.turns) - 1
                ):
                    continue
                retry_key = (session.id, turn_idx_empty)
                tries = _EMPTY_TURN_RETRIES.get(retry_key, 0) + 1
                _EMPTY_TURN_RETRIES[retry_key] = tries
                if tries < _MAX_EMPTY_TURN_RETRIES:
                    continue
                holds.pop(session.id, None)
                cursor.record_broadcast(session.id, turn_idx_empty + 1)
                _EMPTY_TURN_RETRIES.pop(retry_key, None)
                continue

            turn_idx = prev + offset
            is_tail_assistant = (
                turn.role == "assistant"
                and turn_idx == len(session.turns) - 1
            )

            if is_tail_assistant:
                fp = _assistant_turn_fingerprint(turn)
                hold = holds.get(session.id)
                now_m = time.monotonic()
                tool_n = len(turn.tool_calls or [])
                if (
                    hold is not None
                    and hold.turn_idx == turn_idx
                    and fp == hold.fp
                ):
                    if now_m - hold.last_fp_change >= tail_stability_quiet_secs(
                        opts.poll_interval, tool_count=tool_n
                    ):
                        holds.pop(session.id, None)
                        cursor.record_broadcast(session.id, turn_idx + 1)
                        cursor.mark_turn_posted(session.id, turn_idx, turn)
                        _post_assistant_closed(
                            session_poster,
                            session,
                            branch=event_branch,
                            git=event_git,
                            opts=opts,
                            last_assistant_cloud_id=cloud_ids.get(session.id),
                        )
                    continue

            _EMPTY_TURN_RETRIES.pop((session.id, turn_idx), None)

            if not is_tail_assistant and cursor.is_turn_posted(
                session.id, turn_idx, turn
            ):
                cursor.record_broadcast(session.id, turn_idx + 1)
                continue

            ok, created_id = session_poster.send(event)
            if ok:
                posted_count += 1
            if not ok:
                now_m = time.monotonic()
                if now_m - _POST_FAILURE_WARN_MONO[0] >= 60.0:
                    _POST_FAILURE_WARN_MONO[0] = now_m
                    warn(
                        "Spec Live: POST to cloud failed — teammates (and "
                        "`spec team watch`) will not see new prompts until this "
                        f"succeeds (project id {opts.project_id}). Check "
                        "`spec login`, SPEC_API / saved api_base, and "
                        "`.spec/watch.log`. If POST succeeds but SSE is empty, "
                        "your API may be running multiple workers without a "
                        "shared prompt hub."
                    )
                # Network blip — try this turn again next tick. Don't
                # advance the cursor; we'd rather double-deliver
                # (server is idempotent on session_id+role+turn_at)
                # than skip. ``break`` (not ``return``) so other agent
                # sessions in this tick still get a chance to POST.
                break

            if turn.role == "assistant" and created_id is not None:
                cloud_ids[session.id] = created_id

            if not is_tail_assistant:
                cursor.mark_turn_posted(session.id, turn_idx, turn)

            if is_tail_assistant:
                fp = _assistant_turn_fingerprint(turn)
                holds[session.id] = _AssistantTailHold(
                    turn_idx=turn_idx,
                    fp=fp,
                    last_fp_change=time.monotonic(),
                )
                continue

            holds.pop(session.id, None)
            cursor.record_broadcast(session.id, turn_idx + 1)
            if turn.role == "assistant":
                _post_assistant_closed(
                    session_poster,
                    session,
                    branch=event_branch,
                    git=event_git,
                    opts=opts,
                    last_assistant_cloud_id=cloud_ids.get(session.id),
                )

        cursor.clamp_broadcast(session.id, len(session.turns))

    return posted_count


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _establish_live_baseline(
    cursor: LiveCursor,
    bundle_root: Path,
    *,
    machine_wide: bool = False,
) -> int:
    """Mark existing transcript turns as seen before opening the live tail."""
    count = 0
    paths = historical_bundle_paths(bundle_root)
    session_scope = None if machine_wide else paths
    session_since = (
        datetime.now(timezone.utc) - MACHINE_SESSION_LOOKBACK
        if machine_wide
        else None
    )
    sessions = (
        _iter_local_sessions(session_scope, since=session_since)
        if session_since is not None
        else _iter_local_sessions(session_scope)
    )
    for session in sessions:
        # Re-establish this boundary on every process start. Turns appended
        # while Spec was stopped are history relative to this join and must
        # not trickle into the live feed after restart.
        cursor.set_broadcast_horizon(session.id, len(session.turns))
        count += 1
    cursor.mark_producer_baseline()
    cursor.save()
    return count


def _iter_local_sessions(
    paths,
    *,
    since: datetime | None = None,
) -> Iterable[Session]:  # type: ignore[no-untyped-def]
    """Yield freshly-read local sessions across all four adapters.

    ``paths=None`` is the machine-wide source contract used only by the one
    ``spec on`` owner; an iterable preserves repository-scoped capture.

    Each adapter is gated on its store existing — we never error out
    when one isn't installed. ``verbose=True`` ensures assistant turn
    text is available; per-event redaction / truncation happens in
    :func:`_build_outgoing`.

    Sessions are sorted **newest-first** (by ``ended_at`` then
    ``started_at``) so a busy tick still ships the latest prompts to
    Cloud before older backlog — matches reviewer expectations for
    "live" and reduces time-to-visible for the thread you just typed in.
    """
    sessions: list[Session] = []

    if claude_code_store_root().exists():
        try:
            sessions.extend(
                read_claude_code_sessions(paths, since=since, verbose=True)
            )
        except ClaudeCodeError as e:
            log.debug("spec-live: claude_code adapter skipped: %s", e)

    if cursor_workspace_storage_root().exists():
        try:
            sessions.extend(read_cursor_sessions(paths, since=since, verbose=True))
        except CursorError as e:
            log.debug("spec-live: cursor adapter skipped: %s", e)

    if codex_transcript_store_available():
        try:
            sessions.extend(read_codex_sessions(paths, since=since, verbose=True))
        except CodexError as e:
            log.debug("spec-live: codex adapter skipped: %s", e)

    if compress_session_store_root().exists():
        try:
            sessions.extend(
                read_compress_sessions(paths, since=since, verbose=True)
            )
        except CompressError as e:
            log.debug("spec-live: compress adapter skipped: %s", e)

    # Codex forks can expose several rollout files with the same logical
    # session id. They cannot share one cursor safely: a short fork would move
    # the horizon behind the complete transcript and make old turns look new.
    # Consolidate once at the adapter boundary and keep the most complete view.
    by_identity: dict[tuple[str, str], Session] = {}
    for session in sessions:
        key = (session.source, session.id)
        current = by_identity.get(key)
        if current is None or len(session.turns) > len(current.turns):
            by_identity[key] = session
    sessions = list(by_identity.values())

    epoch = datetime.min.replace(tzinfo=timezone.utc)

    def _recency_key(s: Session) -> datetime:
        return s.ended_at or s.started_at or epoch

    sessions.sort(key=_recency_key, reverse=True)
    if paths is None:
        yield from sessions
    else:
        yield from _scoped_sessions(sessions, paths)


def _scoped_sessions(
    sessions: Iterable[Session],
    bundle_paths,
) -> Iterable[Session]:  # type: ignore[no-untyped-def]
    """Route parent-workspace sessions through exactly one local watcher.

    A Codex/Claude/Cursor session launched at a common workspace root used to
    match every registered child bundle.  Every watcher then replayed the same
    prompt into a different Cloud project, leaking unrelated prompts across
    project boundaries and multiplying traffic.  Exact/in-bundle working
    directories remain unambiguous. A parent-workspace session touching one
    child is admitted by that child's watcher. A session touching multiple
    children (or none) is admitted by one deterministic watcher and posted to
    the workspace endpoint, never to an arbitrary project.

    If the current root is not in the registry, preserve the historical
    behavior; that covers direct ``spec watch`` use before machine discovery.
    """
    for session in sessions:
        if _session_route(session, bundle_paths) != "skip":
            yield session


def _session_route(
    session: Session,
    bundle_paths,
) -> str:  # type: ignore[no-untyped-def]
    """Return ``project``, ``workspace``, or ``skip`` for this watcher."""
    paths = [Path(value).expanduser().resolve() for value in bundle_paths]
    if not paths:
        return "skip"
    current = paths[0]
    registered: list[Path] = []
    for raw in load_preferences().bundles:
        try:
            root = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if root not in registered:
            registered.append(root)
    if current not in registered:
        return "project"

    raw_cwd = (session.cwd or "").strip()
    if not raw_cwd:
        return "project"
    try:
        cwd = Path(raw_cwd).expanduser().resolve()
    except OSError:
        return "skip"
    if cwd == current or current in cwd.parents:
        return "project"
    if cwd not in current.parents:
        return "skip"

    candidates = sorted(
        (root for root in registered if root == cwd or cwd in root.parents),
        key=lambda root: str(root).lower(),
    )
    if len(candidates) <= 1:
        return "project"

    touched_candidates: set[Path] = set()
    for raw_path in session.paths_touched or []:
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        candidate_path = Path(raw_path).expanduser()
        if not candidate_path.is_absolute():
            candidate_path = cwd / candidate_path
        try:
            candidate_path = candidate_path.resolve()
        except OSError:
            continue
        for root in candidates:
            if candidate_path == root or root in candidate_path.parents:
                touched_candidates.add(root)

    if len(touched_candidates) == 1:
        return "project" if current in touched_candidates else "skip"
    return "workspace" if current == candidates[0] else "skip"


def _build_outgoing(
    session: Session,
    turn: Turn,
    *,
    branch: str | None,
    git,
    opts: WatcherOptions,
) -> OutgoingEvent | None:
    """Convert one local :class:`Turn` into an :class:`OutgoingEvent`.

    Returns ``None`` when the turn is undeliverable (empty, or assistant
    turn with no body in non-verbose mode). The cursor still advances
    on ``None`` so we don't keep re-considering the same empty turn.

    Redaction: every text field passes through ``redact_text`` (the
    same ``_SECRET_PATTERNS`` used for ``.prompts`` files on disk) so
    bearer tokens, OAuth keys, etc. are never streamed.
    """
    role = turn.role if turn.role in ("user", "assistant") else None
    if role is None:
        return None

    title = redact_text((session.title or "").strip()) or None

    if role == "user":
        body = unwrap_cursor_user_message((turn.text or "").strip())
        if len(body) < MIN_TURN_TEXT_CHARS:
            return None
        body = _truncate(redact_text(body), MAX_TURN_TEXT_CHARS)
        summary = redact_text((turn.summary or "").strip()) or None
        text_out: str | None = body
    else:
        summary = (turn.summary or "").strip() or None
        if summary and is_cursor_redacted_placeholder(summary):
            summary = None
        prose = prose_without_redacted_placeholders((turn.text or "").strip())
        if summary is None and prose:
            summary = prose.splitlines()[0][:300]
        if summary is not None:
            summary = redact_text(summary)
        if opts.verbose_assistant and prose:
            text_out = _truncate(redact_text(prose), MAX_TURN_TEXT_CHARS)
        else:
            text_out = None
        if text_out and is_cursor_redacted_placeholder(text_out):
            text_out = None
        # Tool-only assistant turns (no prose, just tool_use entries
        # like Edit / Write / Bash / Read) used to be dropped entirely
        # — which made the team feed look like prompts vanishing into
        # silence whenever an agent was actually busy editing. We now
        # synthesize a short, deterministic summary listing the tools
        # the AI invoked so receivers always see *something* land for
        # an assistant turn. The exact tool names are far more useful
        # to reviewers ("Edit auth.py") than "(no prose)".
        if not summary and turn.tool_calls:
            summary = _synthesize_tool_summary(turn.tool_calls)
        if not summary and not text_out:
            return None

    tool_calls_out: list[ToolCallPayload] = []
    if role == "assistant":
        for call in turn.tool_calls or []:
            name = getattr(call, "name", None)
            if not isinstance(name, str) or not name:
                continue
            args = getattr(call, "args", None) or {}
            if not isinstance(args, dict):
                args = {}
            status = getattr(call, "status", None)
            tool_calls_out.append(
                ToolCallPayload(
                    name=name,
                    args=dict(args),
                    status=status if isinstance(status, str) else None,
                )
            )

    return OutgoingEvent(
        session_id=session.id,
        source=session.source if session.source in (
            "cursor", "codex", "claude_code", "compress", "manual"
        ) else "manual",
        role=role,
        branch=branch or None,
        commit_sha=git.commit_sha if git else None,
        model=turn.model or session.model,
        phase=turn.phase,
        summary=summary,
        text=text_out,
        title=title,
        cwd=session.cwd,
        paths_touched=list(session.paths_touched or []),
        turn_at=turn.at or _now_utc(),
        tool_calls=tool_calls_out,
        broadcast_client_id=opts.broadcast_client_id,
    )


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n[…truncated…]"


# Sentinel prefix on synthesized tool-only assistant summaries. The
# receiver uses this to identify tool-only assistant turns and route
# them through the critic before deciding whether to render them in
# the stream (they are otherwise too noisy to show unconditionally).
# Keep this stable — any change is a wire compatibility break.
TOOL_SUMMARY_PREFIX = "ran "


def _synthesize_tool_summary(calls: list) -> str | None:
    """Build a one-line summary of which tools an assistant turn invoked.

    Used when the assistant emitted no prose (a very common pattern
    in modern agent loops — Claude Code or Codex will sometimes run a
    long chain of Edit / Read / Bash with no narration). Without this
    fallback the watcher would drop the turn and the team feed would
    show only user prompts, making the AI look silent.

    Output shape:
        ``ran 3 tools: Edit auth.py, Bash "pytest -q", Read main.py (+1 more)``

    For Bash specifically we include the first 80 chars of the
    command so the auto-critic can spot destructive verbs
    (``rm -rf``, ``git reset --hard``) in the live stream and a
    reviewer can intervene *before* the blast radius lands. The
    command text passes through ``redact_text`` so any pasted
    secrets are scrubbed before they reach the wire.
    """
    if not calls:
        return None
    parts: list[str] = []
    for call in calls[:3]:
        name = getattr(call, "name", None)
        if not isinstance(name, str) or not name:
            continue
        args = getattr(call, "args", None)
        detail = _tool_call_detail(name, args)
        parts.append(f"{name}{detail}")
    if not parts:
        return None
    extra = max(0, len(calls) - 3)
    body = ", ".join(parts)
    if extra:
        body += f" (+{extra} more)"
    return f"{TOOL_SUMMARY_PREFIX}{len(calls)} tool{'s' if len(calls) != 1 else ''}: {body}"


def _tool_call_detail(name: str, args: object) -> str:
    """Pick the most informative single-line detail for a tool call.

    Priority by tool:

    * ``Bash`` — first ~80 chars of the ``command`` arg (redacted),
      quoted so the destructive-verb critic can pattern-match.
    * file-edit tools (``Edit``, ``Write``, ``Read``, ``MultiEdit``) —
      the basename of the target file from ``path`` / ``file_path``.
    * ``Grep`` / ``Glob`` — the ``pattern`` arg, quoted.
    * everything else — falls back to ``path`` if present, else
      nothing (the bare tool name is the detail).

    Returned with a leading space (or empty string) so it can be
    concatenated straight onto the tool name.
    """
    if not isinstance(args, dict):
        return ""
    if name == "Bash":
        cmd = args.get("command")
        if isinstance(cmd, str) and cmd.strip():
            snippet = redact_text(cmd.strip())[:80]
            return f' "{snippet}"'
        return ""
    if name in {"Grep", "Glob"}:
        pat = args.get("pattern")
        if isinstance(pat, str) and pat.strip():
            return f' "{pat.strip()[:60]}"'
        return ""
    p = args.get("path") or args.get("file_path") or args.get("file")
    if isinstance(p, str) and p:
        return " " + p.rsplit("/", 1)[-1][:60]
    return ""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["WatcherOptions", "run_watcher"]
