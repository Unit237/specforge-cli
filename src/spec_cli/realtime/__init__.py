"""
Spec Live — real-time prompt sharing.

The CLI side of the live prompt feed. ``spec watch`` runs a small
in-process orchestrator that does two things concurrently:

* **Broadcast** — polls Cursor / Codex / Claude Code / Compress transcripts every
  few seconds, redacts each new turn, and posts it to Cloud's
  ``POST /api/projects/{id}/prompt-events`` endpoint.
* **Receive** — holds a long-lived SSE connection on
  ``GET /api/projects/{id}/prompt-stream`` and surfaces every teammate's
  turn in the user's terminal (and optionally on disk).

This package is the wiring; ``spec_cli.commands.watch`` is the user-
facing surface. See ``spec/PROMPT-LIVE-PLAN.md`` for the full design.
"""

from .daemon import (
    DEFAULT_STOP_GRACE_SECS,
    WATCH_DIR,
    WATCH_LOG_FILENAME,
    WATCH_PID_FILENAME,
    StartOutcome,
    StopOutcome,
    WatcherPidRecord,
    WatcherStartError,
    is_pid_alive,
    is_running,
    read_pid_file,
    remove_pid_file,
    start_in_background,
    stop,
    watch_log_path,
    watch_pid_path,
    write_pid_file,
)
from .daemon import stop as stop_daemon
from .events import IncomingEvent, OutgoingEvent, PresenceFile, PresencePayload
from .mirror import PeerMirror
from .notifier import Notifier
from .presence import (
    LocalPresence,
    PeerPresence,
    PresenceCache,
    compute_local_presence,
)
from .presence_mirror import (
    TEAM_PRESENCE_DIR,
    TEAM_PRESENCE_FILENAME,
    TeamPresenceMirror,
    read_team_presence,
)
from .tracker import LiveCursor
from .transport import HTTPPoster, SSEConsumer, SSEStreamError
from .watcher import WatcherOptions, run_watcher

__all__ = [
    "DEFAULT_STOP_GRACE_SECS",
    "HTTPPoster",
    "IncomingEvent",
    "LiveCursor",
    "LocalPresence",
    "Notifier",
    "OutgoingEvent",
    "PeerMirror",
    "PeerPresence",
    "PresenceCache",
    "PresenceFile",
    "PresencePayload",
    "SSEConsumer",
    "SSEStreamError",
    "StartOutcome",
    "StopOutcome",
    "TEAM_PRESENCE_DIR",
    "TEAM_PRESENCE_FILENAME",
    "TeamPresenceMirror",
    "WATCH_DIR",
    "WATCH_LOG_FILENAME",
    "WATCH_PID_FILENAME",
    "WatcherOptions",
    "WatcherPidRecord",
    "WatcherStartError",
    "compute_local_presence",
    "is_pid_alive",
    "is_running",
    "read_pid_file",
    "read_team_presence",
    "remove_pid_file",
    "run_watcher",
    "start_in_background",
    "stop",
    "stop_daemon",
    "watch_log_path",
    "watch_pid_path",
    "write_pid_file",
]
