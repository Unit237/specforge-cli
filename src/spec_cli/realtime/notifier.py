"""
Terminal output for incoming Spec Live events.

The Notifier is the surface a user actually *sees* when running
``spec watch``. It receives :class:`IncomingEvent` instances from the
SSE consumer (on a background thread) and prints each one in the
shared Rich console.

Output style mirrors the existing `spec` tone — color is signal,
density is low. One line per event in compact mode; one short block
per event in default mode. We deliberately don't draw boxes or tables
— the watcher window is meant to live alongside an editor and a chat,
not be the focal point.

For team review use (``spec team watch``), the Notifier can also
surface :mod:`spec_cli.realtime.critic` suggestions inline so a
reviewer can spot dangerous / vague prompts without reading every
word. The critic is opt-out, not opt-in: catching mistakes is the
whole point of having a stream open.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
from collections import deque
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from rich.markup import escape

from ..prompts.schema import MAX_TURN_TEXT_CHARS
from ..prompts.text_sanitize import (
    is_cursor_redacted_placeholder,
    prose_without_redacted_placeholders,
    unwrap_cursor_user_message,
)
from ..ui import console, flush_streaming_output
from .critic import (
    SEV_HIGH,
    Critique,
    critique_event,
    is_tool_only_summary,
    suggested_flag_command,
)
from .events import IncomingEvent, IncomingFlag, ToolCallPayload

# One label for every command that consumes the account/team-wide stream.
WORKSPACE_FEED_LABEL = "workspace (all bundles)"

# Local wall clock for live panes — include the calendar date so
# overnight / multi-day ``spec team watch`` sessions stay readable.
_LIVE_EVENT_CLOCK_FMT = "%Y-%m-%d %H:%M:%S"


def format_live_event_clock(value: datetime | None) -> str:
    """Format ``value`` in the viewer's local timezone with date + time.

    Matches the historical ``_short_time`` behaviour for naive vs aware
    datetimes (naive values follow :meth:`datetime.datetime.astimezone`
    rules).
    """
    if value is None:
        value = datetime.now(timezone.utc)
    return value.astimezone().strftime(_LIVE_EVENT_CLOCK_FMT)


def _short_cwd(cwd: str | None) -> str | None:
    """Render a teammate's working directory compactly for the header.

    Strips the user's own ``$HOME`` to ``~`` (universal shell ergonomics)
    and collapses very long paths to ``…/last-two-segments`` so the
    header line never wraps. Returns ``None`` for missing / empty
    input so callers can compose the chip conditionally.
    """
    if not isinstance(cwd, str):
        return None
    path = cwd.strip()
    if not path:
        return None
    home = os.path.expanduser("~")
    if home and (path == home or path.startswith(home + os.sep)):
        path = "~" + path[len(home):]
    # Hard cap on chip width — pick the last two segments when the
    # path is longer than 40 chars so the eye still recognises the
    # repo name on the right.
    if len(path) > 40:
        try:
            parts = Path(path).parts
            if len(parts) > 2:
                path = "…/" + "/".join(parts[-2:])
        except Exception:  # noqa: BLE001
            pass
    return path


def _short_session(session_id: str | None) -> str | None:
    """Render a short, stable session badge. We use the first 6 chars
    of the upstream session id — long enough to be unique across the
    handful of concurrent sessions a reviewer is likely to be
    watching, short enough not to dominate the header line."""
    if not isinstance(session_id, str):
        return None
    sid = session_id.strip()
    if not sid:
        return None
    return sid[:6]


def _paths_chip(paths: list[str] | None) -> str | None:
    """Render a compact chip listing the first couple of files an
    event touched, with an overflow marker. Cheap proxy for "what
    did this turn change" without shipping a full diff over the
    wire. Returns ``None`` when there is nothing to show."""
    if not paths:
        return None
    seen = [p for p in paths if isinstance(p, str) and p][:2]
    if not seen:
        return None
    extra = max(0, len(paths) - len(seen))
    basenames = [p.rsplit("/", 1)[-1] for p in seen]
    body = ", ".join(basenames)
    if extra:
        body += f", +{extra} more"
    return body


# Palette for stable session colorization. Each session id hashes to
# one of these hues so all events from the same thread share a chip
# color in the pane — essential when two or three teammates are
# prompting concurrently and the events interleave. The palette stays
# within the "muted but distinct" zone so the colored chip never
# competes with the USER / AI badges for attention.
_SESSION_PALETTE = (
    "#7de3ff",  # cyan
    "#9ee37d",  # lime
    "#c79bff",  # purple
    "#ffb86b",  # amber
    "#ff8aa3",  # pink
    "#7da3ff",  # blue
    "#f6d57e",  # gold
    "#7de3c2",  # mint
)


def _session_color(session_id: str | None) -> str:
    """Stable color for a session chip. Hash → palette index. Same id
    always picks the same color across runs and machines."""
    if not isinstance(session_id, str) or not session_id.strip():
        return "#9aa3b2"
    digest = hashlib.sha256(session_id.strip().encode("utf-8")).digest()
    return _SESSION_PALETTE[digest[0] % len(_SESSION_PALETTE)]


def _event_context_line(event: IncomingEvent) -> str:
    """Stable chat identity plus optional repository context for one row."""
    session_color = _session_color(event.session_id)
    parts: list[str] = []
    title = (event.title or "").strip()
    if title:
        parts.append(
            f"[sf.muted]chat[/] [bold {session_color}]"
            f"{escape(_truncate(title, 72))}[/]"
        )
    cwd_chip = _short_cwd(event.cwd)
    if cwd_chip:
        parts.append(f"[sf.muted]cwd[/] [sf.label]{escape(cwd_chip)}[/]")
    paths_chip = _paths_chip(event.paths_touched)
    if paths_chip:
        parts.append(
            f"[sf.muted]touched[/] [sf.label]{escape(paths_chip)}[/]"
        )
    session_chip = _short_session(event.session_id)
    if session_chip:
        parts.append(
            f"[sf.muted]session[/] [bold {session_color}]"
            f"{escape(session_chip)}[/]"
        )
    return "  ".join(parts)


# Markdown / pasted-log code blocks the user explicitly does *not*
# want in the default ``spec team watch`` view. Matches fenced
# ```lang …``` blocks (greedy across newlines), plus 4-space-indented
# log dumps that often come back as quoted error output.
_CODE_FENCE_RE = re.compile(
    r"```([a-zA-Z0-9_+\-]*)\n?(.*?)```",
    re.DOTALL,
)


def _strip_code_blocks(text: str) -> str:
    """Replace fenced code blocks with a compact ``[code: lang ~N lines]``
    placeholder so the default ``spec team watch`` view shows the
    prose narration without pages of pasted code or tool output.

    The user toggle for "show me the code edits too" is the
    ``--show-tool-runs`` flag — distinct from prose code blocks
    because those are explanatory examples inside the AI's reply, not
    tool invocations against the repo. We collapse both so the
    pane stays scannable; reviewers who want the raw body fetch the
    event by id, or run ``spec team watch --show-tool-runs`` for the
    structured tool list.
    """
    def _replace(match: re.Match[str]) -> str:
        lang = (match.group(1) or "").strip().lower()
        body = match.group(2) or ""
        lines = body.count("\n") + (1 if body.strip() else 0)
        if lang:
            return f"[code: {lang} ~{lines} line{'s' if lines != 1 else ''}]"
        return f"[code ~{lines} line{'s' if lines != 1 else ''}]"

    return _CODE_FENCE_RE.sub(_replace, text)


def _format_tool_call_line(call: ToolCallPayload) -> str:
    """One-line summary of a tool invocation for the team watch pane.

    Echoes the same shape the broadcaster's synthesized ``ran N
    tools:`` summary uses (``Edit auth.py``, ``Bash "pytest -q"``,
    ``Grep "TODO"``) so a reviewer's eye recognises both as the same
    kind of artefact. Long arg values are clipped at 80 chars so the
    line stays terminal-friendly.
    """
    name = call.name
    args = call.args or {}
    detail = ""
    if name == "Bash":
        cmd = args.get("command")
        if isinstance(cmd, str) and cmd.strip():
            detail = f' "{cmd.strip()[:80]}"'
    elif name in {"Grep", "Glob"}:
        pat = args.get("pattern")
        if isinstance(pat, str) and pat.strip():
            detail = f' "{pat.strip()[:60]}"'
    elif name == "WebSearch":
        term = args.get("search_term") or args.get("query")
        if isinstance(term, str) and term.strip():
            detail = f' "{term.strip()[:60]}"'
    elif name == "WebFetch":
        url = args.get("url")
        if isinstance(url, str) and url.strip():
            detail = f' {url.strip()[:80]}'
    elif name == "Edit":
        path = args.get("path") or args.get("file_path") or args.get("file")
        new_head = args.get("new_head")
        if isinstance(path, str) and path:
            base = path.rsplit("/", 1)[-1][:60]
            detail = f" {base}"
            if isinstance(new_head, str) and new_head.strip():
                detail += f" → {new_head.strip()[:40]}"
    else:
        path = args.get("path") or args.get("file_path") or args.get("file")
        if isinstance(path, str) and path:
            detail = " " + path.rsplit("/", 1)[-1][:60]
    return f"{name}{detail}"


# Per-kind glyph + color hint used both in the watcher and `spec team
# watch`. Kept small and stable so muscle memory transfers between
# screens.
_FLAG_GLYPH = {
    "warning": ("⚠", "sf.warn"),
    "question": ("?", "sf.point"),
    "block": ("⛔", "sf.reject"),
    "ack": ("✓", "sf.mint"),
}


# Role badges. The point is *zero ambiguity at a glance* — a reviewer
# scanning a busy pane should never have to read text to know whether
# a frame is a human prompt or the AI's reply. Rendered as a chunky
# colored badge with bright background; the role and the role color
# move together (green = human, cyan = AI) so even if a reviewer's
# terminal collapses spaces or wraps, the colour itself disambiguates.
#
# Background colors are inlined as hex so we don't depend on the
# theme having a "bg" variant — these need to render correctly in
# every Rich-capable terminal.
_USER_BADGE = "[bold black on #3ddab4] USER [/]"
_AI_BADGE = "[bold black on #7de3ff]  AI  [/]"
_UPDATE_BADGE = "[bold white on #536273] UPDATE [/]"
_ANSWER_BADGE = "[bold black on #7de3ff] ANSWER [/]"
# Red ERROR badge used when an adapter ships ``role = "error"`` —
# agent timeout, refused request, tool failure. Lights up the pane so
# a reviewer notices an agent in trouble without having to read text.
_ERROR_BADGE = "[bold white on #d63a4e] ERROR [/]"


def _assistant_badge_and_relation(phase: str | None) -> tuple[str, str]:
    """Map source-level delivery phase to visible, accessible semantics."""
    if phase == "commentary":
        return _UPDATE_BADGE, "progress for"
    if phase == "final_answer":
        return _ANSWER_BADGE, "answer to"
    return _AI_BADGE, "replying to"

# Source → display color. Each adapter the watcher can stream from
# gets its own muted-but-distinct hue so a reviewer can tell which
# tool the engineer is using without parsing the label. Falls back
# to the generic muted style for any source we haven't tagged yet.
_SOURCE_COLOR = {
    "claude_code": "#c79bff",   # purple — Claude Code
    "codex": "#9ee37d",         # lime — Codex Desktop / Cursor agent
    "cursor": "#7de3ff",        # cyan — Cursor chat
    "manual": "#c7c9d1",        # neutral — `spec post` / scripted
}


def _source_label(source: str) -> str:
    color = _SOURCE_COLOR.get(source, "#9aa3b2")
    return f"[bold {color}]{source}[/]"


def _short_time(value: datetime | None) -> str:
    return format_live_event_clock(value)


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


# Terminal preview limits (chars, excluding the trailing ellipsis).
# Defaults favour team review: long user prompts and large assistant
# replies stay readable without ``--compact``; compact mode stays
# bounded so one-line logging stays usable.
_PREVIEW_USER = (48_000, 2_000)  # (non-compact, compact)
# Match schema / capture adapters so long in-chat code blocks are not
# clipped in the pane before the wire payload would be.
_PREVIEW_ASSISTANT = (MAX_TURN_TEXT_CHARS, 12_000)  # (non-compact, compact)
_PREVIEW_ERROR = (24_000, 800)

# How long we wait for an assistant follow-up to a user prompt before
# we surface a "no AI reply seen yet" hint. 90 seconds is a sweet
# spot in practice: short enough that a hung agent is noticed
# quickly, long enough that long-running tools (large file reads,
# Bash commands) don't trigger false alarms.
_NO_REPLY_AGE_SECS = 90.0

# Cap on tracked open sessions to keep memory bounded on a very busy
# workspace. We evict the oldest pending pair when over.
_OPEN_SESSIONS_MAX = 256


def _resolve_system_pager_argv() -> list[str] | None:
    """Argv for ``subprocess.run`` to page plain text, or ``None`` to inline."""
    raw = (os.environ.get("SPEC_TEAM_WATCH_PAGER") or "").strip()
    if raw:
        return shlex.split(raw)
    pg = (os.environ.get("PAGER") or "").strip()
    if pg:
        return shlex.split(pg)
    less = shutil.which("less")
    if less:
        return [less, "-R", "-X", "+1"]
    more = shutil.which("more")
    if more:
        return [more]
    return None


class Notifier:
    """Thread-safe printer for incoming Spec Live events.

    Rich's console serializes its own writes, so we don't need an
    explicit lock for ordering — but we do hold a lock around the
    multi-line composed output to keep events from interleaving when
    bursts arrive.

    Two opt-in review aids:

    * ``critic_enabled`` (default True) — runs the rule-based
      :mod:`spec_cli.realtime.critic` on every user turn and prints
      suggestions inline with the exact ``spec team flag`` command
      pre-filled. Disable in dashboards / CI to keep the log clean.

    * **No-reply hint** — when a user prompt has been visible for
      ``_NO_REPLY_AGE_SECS`` and no assistant turn from the same
      ``(project_id, session_id)`` has arrived, the next event from
      that author (or a ping from the heartbeat path) prints a
      ``waiting for AI reply`` warning. Catches the "I see prompts
      but not replies" scenario the team has been hitting.
    """

    def __init__(
        self,
        *,
        compact: bool = False,
        critic_enabled: bool = True,
        notify: bool = False,
        pairing_buffer: Any = None,
        viewer_handle: str | None = None,
        show_tool_runs: bool = False,
        strip_code_blocks: bool = True,
        review_feed_full_bodies: bool = False,
        assistant_live_cap: int | None = None,
        self_user_id: int | None = None,
        local_broadcast_client_id: str | None = None,
    ) -> None:
        self._compact = compact
        self._lock = threading.Lock()
        self._critic_enabled = critic_enabled
        # ``--show-tool-runs`` (off by default): when True, the
        # notifier appends a bullet list of the assistant turn's
        # structured ``tool_calls`` (Edit foo.py, Bash "pytest -q",
        # Read main.py …) under the prose body. Off keeps the pane
        # scannable — most reviewers want prose, not the full edit
        # trail.
        self._show_tool_runs = show_tool_runs
        # ``strip_code_blocks`` (on by default): when True, fenced
        # code blocks in assistant prose are replaced with a compact
        # ``[code: lang ~N lines]`` placeholder. The user's request
        # is "full ai output (without code) by default"; this is
        # how we honour it. Flip off to see the raw prose verbatim.
        self._strip_code_blocks = strip_code_blocks
        # Opt-in attention helper: ring the terminal bell + best-effort
        # OS notification (macOS only for now via ``osascript``) when
        # the critic fires at ``block`` severity, e.g. a teammate just
        # typed ``rm -rf`` or pasted a secret. Default off so noisy
        # team feeds don't beep constantly.
        self._notify = notify
        # session_key → (event_id, posted_at, author_display, warned)
        # Tracks open user prompts that have not yet seen an assistant
        # follow-up; lets ``check_open_sessions`` surface a hint when
        # the AI has been silent too long.
        self._open_sessions: dict[
            tuple[int, str], tuple[int, datetime, str, bool]
        ] = {}
        # (project_id, session_id) → (author_display, truncated prompt) so
        # the first assistant/error line after a user turn can echo what
        # question was being answered — even when the viewer only tuned
        # in after the USER badge scrolled away.
        self._pending_user_prompt: dict[
            tuple[int, str], tuple[str, str]
        ] = {}
        # Optional bounded deque of recent events (``spec team watch``).
        # When the warm-up window skipped the triggering USER row, we
        # still recover the prompt for ``⤷ prompt`` by scanning back.
        self._pairing_buffer = pairing_buffer
        # ``spec team watch`` (non-compact): widen user / error preview
        # toward the schema wire cap. Assistant prose uses
        # :attr:`_assistant_live_cap` when set (digest mode) so the live
        # pane stays scannable; ``/turn`` / ``/full`` print the stored body.
        self._review_feed_full_bodies = review_feed_full_bodies
        # When set (e.g. 400), assistant ``show`` / ``show_completed_pair``
        # prose is capped after merging ``text`` + ``summary`` sensibly.
        # ``None`` means use :meth:`_assistant_body_limit_chars` rules.
        self._assistant_live_cap = assistant_live_cap
        # Latest completed pair for ``/turn`` with no args (digest mode).
        self._last_turn_digest: tuple[str, int, int, int] | None = None
        # Signed-in viewer (``spec team watch`` only). Skip no-reply
        # tracking for your own user prompts — the hint is for teammates.
        self._viewer_handle = (viewer_handle or "").strip().lower() or None
        # ``spec team watch`` QA coalescing stores the last merged pairs so a
        # future slash command (e.g. summary → full body) can re-print without
        # another Cloud round trip.
        self._recent_completed_pairs: deque[
            tuple[IncomingEvent, IncomingEvent]
        ] = deque(maxlen=50)
        # ``spec watch``: signed-in user id + this bundle's install id so
        # we can label same-account traffic from a different computer.
        self._self_user_id = self_user_id
        self._local_broadcast_client_id = (
            (local_broadcast_client_id or "").strip() or None
        )
        # ``/turn`` / ``/full``: while a system pager (``less``) owns the
        # screen, suppress live stream prints so output does not interleave.
        self._live_suppress = False
        self._skipped_while_suppressed = 0

    @staticmethod
    def _user_visible_text(event: IncomingEvent) -> str:
        """Human prompt body for display and ``⤷ prompt`` pairing."""
        raw = (event.text or event.summary or "").strip()
        if event.source == "cursor" and raw:
            return unwrap_cursor_user_message(raw)
        return raw

    @staticmethod
    def _assistant_visible_prose(text: str | None, summary: str | None) -> str:
        """Prefer the stored assistant ``text`` (full model reply body).

        A *short* ``summary`` headline (≤400 chars) may be prepended when it
        is not already the opening of ``text``. Long summaries are never
        pasted above the body — reviewers and ``/turn`` should rely on verbose
        assistant ``text`` rows from the broadcaster, not headline walls.

        Cursor often stores ``text: "[REDACTED]"`` on tool steps while the
        readable headline lives in ``summary`` — never prefer the placeholder
        over real prose.
        """
        t = prose_without_redacted_placeholders((text or "").strip())
        s = prose_without_redacted_placeholders((summary or "").strip())
        if not t:
            return s
        if not s:
            return t
        if s in t:
            return t
        if t in s:
            return s
        if len(s) <= 400 and not t.startswith(s[: min(len(s), 120)]):
            return f"{s}\n\n{t}"
        return t

    @staticmethod
    def _assistant_preview_is_meaningful(text: str | None, summary: str | None) -> bool:
        """True when :meth:`_assistant_visible_prose` would show readable text."""
        body = Notifier._assistant_visible_prose(text, summary).strip()
        return bool(body) and not is_cursor_redacted_placeholder(body)

    def last_turn_digest(self) -> tuple[str, int, int, int] | None:
        """``(session_id, project_id, user_event_id, assistant_event_id)``."""
        return self._last_turn_digest

    def set_critic_enabled(self, enabled: bool) -> None:
        """Toggle the auto-critic at runtime. Used by the ``/critic``
        slash command so a reviewer can silence the suggestion stream
        without restarting the watcher."""
        self._critic_enabled = bool(enabled)

    def _user_preview_limit(self) -> int:
        if self._review_feed_full_bodies and not self._compact:
            return MAX_TURN_TEXT_CHARS
        lim_u, lim_uc = _PREVIEW_USER
        return lim_uc if self._compact else lim_u

    def _error_preview_limit(self) -> int:
        if self._review_feed_full_bodies and not self._compact:
            return MAX_TURN_TEXT_CHARS
        lim_e, lim_ec = _PREVIEW_ERROR
        return lim_ec if self._compact else lim_e

    def _assistant_body_limit_chars(self) -> int:
        """Assistant prose cap before ``…`` truncation in the pane.

        Digest mode (``spec team watch`` without ``--show-tool-runs``)
        sets :attr:`_assistant_live_cap` (~400 chars) for **per-row**
        ``show()`` output when that path is used. Coalesced
        ``show_completed_pair`` uses :meth:`_assistant_body_limit_for_completed_pair`
        instead so a finished turn matches ``spec watch`` (full stored
        prose up to :data:`MAX_TURN_TEXT_CHARS`). ``--show-tool-runs``
        keeps the schema wire cap everywhere.
        """
        if self._assistant_live_cap is not None:
            return int(self._assistant_live_cap)
        if self._show_tool_runs:
            return MAX_TURN_TEXT_CHARS
        if self._review_feed_full_bodies and not self._compact:
            return MAX_TURN_TEXT_CHARS
        return _PREVIEW_ASSISTANT[1] if self._compact else _PREVIEW_ASSISTANT[0]

    def _assistant_body_limit_for_completed_pair(self) -> int:
        """Cap for merged assistant text in ``show_completed_pair``.

        Default ``spec team watch`` buffers streaming chunks and only
        prints the merged reply at flush; that block is the canonical
        view of the turn, so we must not apply the live digest cap here
        (``spec watch`` never applies that cap on assistant bodies).
        """
        if self._assistant_live_cap is not None:
            return MAX_TURN_TEXT_CHARS
        return self._assistant_body_limit_chars()

    def record_pairing(self, event: IncomingEvent) -> None:
        """Update the user→AI pairing tracker without rendering.

        Called from the watcher's ``_deliver`` *before* the filter that
        drops noisy tool-only assistant frames. Without this hop, a
        tool-only assistant reply (synthesized summary, no critic hit)
        would be filtered out before ``show()`` runs — and the
        ``_remember_open_session`` entry left by the matching user
        prompt would never be cleared, causing the no-reply hint to
        fire 90 s later even though the AI *did* reply.

        Safe to call multiple times for the same event: the
        underlying maps are idempotent.
        """
        if event.role == "user":
            self._remember_open_session(event)
        elif event.role in ("assistant", "error", "assistant_closed"):
            self._mark_session_replied(event)

    @staticmethod
    def _session_pair_key(event: IncomingEvent) -> tuple[int, str]:
        sid = (event.session_id or "").strip()
        if not sid:
            # Extremely rare — keep keys stable per row so we never leak
            # one session's prompt into another on the same project.
            sid = f"_ev:{event.id}"
        return (event.project_id, sid)

    def _pairing_prompt_from_buffer(
        self, event: IncomingEvent
    ) -> tuple[str, str] | None:
        """Find the most recent user turn for this session with ``id``
        strictly before ``event`` — used when ``_pending_user_prompt``
        missed the USER frame (bootstrap gap, reconnect, or bursty
        assistant rows).

        Stops walking back if we encounter another assistant/error
        row for the same session first — that one already carried the
        echo, so this row is a continuation and should *not* repeat
        the prompt one-liner.
        """
        buf = self._pairing_buffer
        if buf is None:
            return None
        key = self._session_pair_key(event)
        try:
            tail = reversed(buf)
        except TypeError:
            return None
        for ev in tail:
            if ev.id >= event.id:
                continue
            if self._session_pair_key(ev) != key:
                continue
            # If a prior assistant/error in the same session is closer
            # to ``event`` than the user prompt, this row is a chain
            # continuation — the echo already happened, suppress it.
            if ev.role in ("assistant", "error", "assistant_closed"):
                return None
            if ev.role != "user":
                continue
            preview = self._user_visible_text(ev)
            if not preview:
                continue
            lim_u = self._user_preview_limit()
            preview = _truncate(preview, lim_u)
            return (ev.author_display, preview)
        return None

    def _other_machine_note(self, event: IncomingEvent) -> str:
        """Tag same-account events from a different ``spec watch`` install."""
        lb = self._local_broadcast_client_id
        wb = (event.broadcast_client_id or "").strip()
        if not lb or not wb or wb == lb:
            return ""
        vh = self._viewer_handle
        if vh:
            ah = (event.author_handle or "").strip().lstrip("@").lower()
            if ah == vh:
                return " [sf.warn]· other machine[/]"
        if self._self_user_id is not None and event.author_user_id == self._self_user_id:
            return " [sf.warn]· other machine[/]"
        return ""

    def show(self, event: IncomingEvent) -> None:
        # Completion frames exist only to flush coalescers. Rendering them as
        # assistant messages produced the empty AI rows that buried real chat.
        if event.role == "assistant_closed":
            self.record_pairing(event)
            return
        # Non-dangerous tool-only rows obey the same opt-in contract as the
        # structured digest. The critic can still break through this filter.
        if (
            event.role == "assistant"
            and is_tool_only_summary(event.summary)
            and not self._show_tool_runs
            and not (self._critic_enabled and critique_event(event))
        ):
            self.record_pairing(event)
            return

        time_label = _short_time(event.turn_at or event.received_at)
        author = event.author_display
        om = self._other_machine_note(event)
        branch = event.branch or "-"
        source_label = _source_label(event.source)
        bundle = (
            f" [sf.muted]· {event.bundle_label}[/]"
            if event.bundle_label
            else ""
        )

        # Composable context chips — cwd shortened to ``~``, paths the
        # turn touched, and a short session id for thread tracking.
        # All three are optional: we only render the divider before a
        # chip when it actually has content, so a quiet stream stays
        # clean and a busy stream still fits on one line.
        ctx_line = _event_context_line(event)

        critiques: list[Critique] = []
        pair_key = self._session_pair_key(event)
        pending_prompt: tuple[str, str] | None = None
        if event.role == "user":
            preview = self._user_visible_text(event)
            preview = _truncate(preview, self._user_preview_limit())
            # USER badge (mint background) + author handle in the
            # source's accent color. A reviewer scanning a fast pane
            # sees the green block and knows immediately a human just
            # typed something.
            head = (
                f"{_USER_BADGE} [bold #3ddab4]{author}[/]{om} "
                f"[sf.muted]· prompt to[/] {source_label} "
                f"[sf.muted]· {branch} · {time_label}[/]"
                f"{bundle}"
            )
            if self._critic_enabled:
                critiques = critique_event(event)
            if preview:
                self._pending_user_prompt[pair_key] = (author, preview)
            # Remember this prompt as "awaiting AI reply". Sessions
            # are pinned by (project_id, session_id) — the same
            # identity the server uses for dedupe.
            self._remember_open_session(event)
        elif event.role == "error":
            # Agent error: timeout / tool failure / refused request.
            # Red badge + short message in the header keeps the eye
            # snapping to it even on a busy pane.
            preview = (event.text or event.summary or "").strip()
            preview = _truncate(preview, self._error_preview_limit())
            model = event.model or "agent"
            head = (
                f"{_ERROR_BADGE} [bold #ff8a98]{model}[/] "
                f"[sf.muted]· failed on[/] [bold #3ddab4]{author}[/]{om} "
                f"[sf.muted]· in[/] {source_label} "
                f"[sf.muted]· {branch} · {time_label}[/]"
                f"{bundle}"
            )
            # An error closes the awaiting-reply tracker for this
            # session — we have a definitive answer, just not a happy
            # one.
            self._mark_session_replied(event)
            # Surface assistant-side critic on the error message too,
            # so e.g. a tool failure containing destructive text
            # still gets flagged.
            if self._critic_enabled:
                critiques = critique_event(event)
            pending_prompt = self._pending_user_prompt.pop(pair_key, None)
            if pending_prompt is None:
                pending_prompt = self._pairing_prompt_from_buffer(event)
        else:
            # Prefer full ``text`` over ``summary`` — both are usually set
            # for assistant turns, and the summary is only a headline.
            raw = self._assistant_visible_prose(event.text, event.summary)
            preview = raw.strip()
            # By default, fenced code blocks collapse to a compact
            # ``[code: lang ~N lines]`` placeholder. The user pulls the
            # raw body back via ``--show-tool-runs`` or by setting
            # ``strip_code_blocks=False`` on the notifier; either way
            # the pane stays scannable on default settings.
            if preview and self._strip_code_blocks and not self._show_tool_runs:
                preview = _strip_code_blocks(preview)
            preview = _truncate(preview, self._assistant_body_limit_chars())
            model = event.model or "assistant"
            # AI badge (cyan background). The model name carries the
            # source's accent color so "claude_code/claude-sonnet-4"
            # and "codex/gpt-5" read as cleanly separable identities.
            badge, relation = _assistant_badge_and_relation(event.phase)
            head = (
                f"{badge} [bold #7de3ff]{model}[/] "
                f"[sf.muted]· {relation}[/] [bold #3ddab4]{author}[/]{om} "
                f"[sf.muted]· in[/] {source_label} "
                f"[sf.muted]· {branch} · {time_label}[/]"
                f"{bundle}"
            )
            # Pair off the awaiting-reply tracker.
            self._mark_session_replied(event)
            # Assistant turns also run through the critic so
            # destructive Bash / test-bypass language in tool
            # summaries surfaces in the live stream.
            if self._critic_enabled:
                critiques = critique_event(event)
            pending_prompt = self._pending_user_prompt.pop(pair_key, None)
            if pending_prompt is None:
                pending_prompt = self._pairing_prompt_from_buffer(event)

        with self._lock:
            if self._live_suppress:
                self._skipped_while_suppressed += 1
                return
            if self._compact:
                # Compact mode lives on one line — context chips ride
                # at the end so the row still parses even when piped
                # into ``grep`` for a handle / file / session id.
                tail = ""
                if preview:
                    flat = " ".join(preview.splitlines())
                    tail = f"  {escape(flat)}"
                if pending_prompt:
                    _, prev_txt = pending_prompt
                    tail = f"  [sf.muted]⤷ {prev_txt}[/]{tail}"
                ctx_compact = f"  {ctx_line}" if ctx_line else ""
                console.print(f"{head}{tail}{ctx_compact}")
                # Compact is still one summary line for the header + body;
                # ``--show-tool-runs`` would otherwise never reach
                # ``_render_tool_calls`` (we used to ``return`` early).
                if (
                    event.role == "assistant"
                    and self._show_tool_runs
                    and event.tool_calls
                ):
                    self._render_tool_calls(event.tool_calls)
                self._render_critiques(event, critiques)
                flush_streaming_output()
                return
            console.print()
            console.print(head)
            if ctx_line:
                # One indented line below the badge with the muted
                # context chips. Always indented to the same column
                # as the body so a vertical scan groups header →
                # context → body cleanly.
                console.print(f"  {ctx_line}")
            if pending_prompt:
                _, prev_txt = pending_prompt
                console.print(
                    f"  [sf.muted]⤷ prompt ·[/] [sf.label]{prev_txt}[/]"
                )
            if preview:
                # Indent assistant / error bodies a bit further so
                # they read as a clear "reply" block underneath the
                # header.
                indent = "    " if event.role != "user" else "  "
                for line in preview.splitlines():
                    # Literal ``[...]`` in pasted logs / code must not be
                    # parsed as Rich markup (team watch often carries
                    # terminal scrollback with brackets and backticks).
                    console.print(
                        f"{indent}{line}", markup=False, highlight=False
                    )
            elif event.role == "assistant":
                if self._show_tool_runs and event.tool_calls:
                    console.print(
                        "    [sf.muted](no prose on this row — structured tool "
                        "runs below)[/]"
                    )
                else:
                    console.print(
                        "    [sf.muted](assistant body not on wire — enable "
                        "``cloud.prompt_stream.verbose`` on the broadcaster's "
                        "bundle and default ``spec team watch`` verbosity, or "
                        "only a summary was posted)[/]"
                    )
            if (
                event.role == "assistant"
                and self._show_tool_runs
                and event.tool_calls
            ):
                # ``--show-tool-runs`` mode: append the full ordered
                # tool invocation list below the prose. Indented two
                # spaces deeper than the body so the eye groups
                # "narration" / "what the AI did" visually.
                self._render_tool_calls(event.tool_calls)
            self._render_critiques(event, critiques)
            flush_streaming_output()

    def show_completed_pair(
        self,
        user: IncomingEvent,
        assistant: IncomingEvent,
    ) -> None:
        """Print the user prompt again bundled with the merged assistant reply.

        Used by ``spec team watch`` after the user already saw their prompt
        immediately: this second block is the readable Q/A unit once
        streaming has gone quiet. The first ``show(user)`` already ran the
        auto-critic — we skip critic on the echoed user row to avoid
        duplicate noise.

        Appends to :attr:`_recent_completed_pairs` for a future in-pane
        ``/expand`` (summary → full body) without re-fetching Cloud.
        """
        self._recent_completed_pairs.append((user, assistant))

        u_author = user.author_display
        u_branch = user.branch or "-"
        u_src = _source_label(user.source)
        u_bundle = (
            f" [sf.muted]· {user.bundle_label}[/]" if user.bundle_label else ""
        )
        u_time = _short_time(user.turn_at or user.received_at)
        u_om = self._other_machine_note(user)
        u_head = (
            f"{_USER_BADGE} [bold #3ddab4]{u_author}[/]{u_om} "
            f"[sf.muted]· prompt to[/] {u_src} "
            f"[sf.muted]· {u_branch} · {u_time}[/]"
            f"{u_bundle}"
        )
        u_preview_raw = self._user_visible_text(user)
        u_preview = _truncate(u_preview_raw, self._user_preview_limit())
        u_ctx = _event_context_line(user)

        a_author = assistant.author_display
        a_branch = assistant.branch or "-"
        a_src = _source_label(assistant.source)
        a_bundle = (
            f" [sf.muted]· {assistant.bundle_label}[/]"
            if assistant.bundle_label
            else ""
        )
        a_time = _short_time(assistant.turn_at or assistant.received_at)
        model = assistant.model or "assistant"
        a_om = self._other_machine_note(assistant)
        a_badge, a_relation = _assistant_badge_and_relation(assistant.phase)
        a_head = (
            f"{a_badge} [bold #7de3ff]{model}[/] "
            f"[sf.muted]· {a_relation}[/] [bold #3ddab4]{a_author}[/]{a_om} "
            f"[sf.muted]· in[/] {a_src} "
            f"[sf.muted]· {a_branch} · {a_time}[/]"
            f"{a_bundle}"
        )
        a_preview = self._assistant_visible_prose(
            assistant.text, assistant.summary
        ).strip()
        if a_preview and self._strip_code_blocks and not self._show_tool_runs:
            a_preview = _strip_code_blocks(a_preview)
        a_preview = _truncate(
            a_preview, self._assistant_body_limit_for_completed_pair()
        )
        a_ctx = _event_context_line(assistant)
        pending_line = (u_author, u_preview) if u_preview else None

        a_critiques: list[Critique] = []
        if self._critic_enabled:
            a_critiques = critique_event(assistant)

        with self._lock:
            if self._live_suppress:
                self._skipped_while_suppressed += 1
                return
            console.print()
            console.print(
                f"  [sf.muted]· paired reply ·[/] "
                f"[sf.label]#{user.id}[/] [sf.muted]→[/] "
                f"[sf.label]#{assistant.id}[/]"
            )
            if self._compact:
                u_tail = ""
                if u_preview:
                    u_tail = f"  {escape(' '.join(u_preview.splitlines()))}"
                u_ctx_c = f"  {u_ctx}" if u_ctx else ""
                console.print(f"{u_head}{u_tail}{u_ctx_c}")
                a_tail = ""
                if a_preview:
                    a_tail = f"  {escape(' '.join(a_preview.splitlines()))}"
                if pending_line:
                    _, prev_txt = pending_line
                    a_tail = f"  [sf.muted]⤷ {prev_txt}[/]{a_tail}"
                a_ctx_c = f"  {a_ctx}" if a_ctx else ""
                console.print(f"{a_head}{a_tail}{a_ctx_c}")
                if self._show_tool_runs and assistant.tool_calls:
                    self._render_tool_calls(assistant.tool_calls)
                self._render_critiques(assistant, a_critiques)
                if self._assistant_live_cap is not None:
                    sid = (user.session_id or "").strip()
                    chip = _short_session(sid) or (sid[:8] if sid else "?")
                    self._last_turn_digest = (
                        sid,
                        user.project_id,
                        user.id,
                        assistant.id,
                    )
                    console.print(
                        f"  [bold #9ee37d]● turn complete[/]  "
                        f"[sf.label]/turn {chip}[/]  [sf.muted]·[/]  "
                        f"[sf.label]/full {chip}[/]"
                    )
                return

            console.print()
            console.print(u_head)
            if u_ctx:
                console.print(f"  {u_ctx}")
            if u_preview:
                for line in u_preview.splitlines():
                    console.print(
                        f"  {line}", markup=False, highlight=False
                    )

            console.print()
            console.print(a_head)
            if a_ctx:
                console.print(f"  {a_ctx}")
            if pending_line:
                _, prev_txt = pending_line
                console.print(
                    f"  [sf.muted]⤷ prompt ·[/] [sf.label]{prev_txt}[/]"
                )
            if a_preview:
                for line in a_preview.splitlines():
                    console.print(
                        f"    {line}", markup=False, highlight=False
                    )
            elif assistant.role == "assistant":
                if self._show_tool_runs and assistant.tool_calls:
                    console.print(
                        "    [sf.muted](no prose on this row — structured tool "
                        "runs below)[/]"
                    )
                else:
                    console.print(
                        "    [sf.muted](assistant body not on wire — enable "
                        "``cloud.prompt_stream.verbose`` on the broadcaster's "
                        "bundle and default ``spec team watch`` verbosity, or "
                        "only a summary was posted)[/]"
                    )
            if self._show_tool_runs and assistant.tool_calls:
                self._render_tool_calls(assistant.tool_calls)
            self._render_critiques(assistant, a_critiques)
            if self._assistant_live_cap is not None:
                sid = (user.session_id or "").strip()
                chip = _short_session(sid) or (sid[:8] if sid else "?")
                self._last_turn_digest = (sid, user.project_id, user.id, assistant.id)
                console.print(
                    f"  [bold #9ee37d]● turn complete[/]  [sf.muted]#"
                    f"{user.id} → #{assistant.id} · session[/] [sf.label]{chip}[/]"
                )
                console.print(
                    "  [sf.muted]expand this reply:[/] [sf.label]/turn "
                    f"{chip}[/]   [sf.muted]whole session:[/] [sf.label]/full "
                    f"{chip}[/]"
                )
            flush_streaming_output()

    def _render_tool_calls(self, calls: list[ToolCallPayload]) -> None:
        """Print a compact, structured tool digest under the assistant body.

        Calls are grouped by canonical tool name with a few ordered examples
        per group.  This preserves the important review signal (what kinds of
        actions ran, how often, and representative targets) without turning a
        busy agent loop into dozens of visually identical terminal rows.
        """
        if not calls:
            return
        n = len(calls)
        grouped: dict[str, list[str]] = {}
        for call in calls:
            grouped.setdefault(call.name, []).append(_format_tool_call_line(call))
        counts = " · ".join(
            f"{name} ×{len(lines)}" for name, lines in grouped.items()
        )
        console.print(
            f"    [sf.muted]» {n} tool run{'s' if n != 1 else ''} · "
            f"{escape(counts)}[/]",
            highlight=False,
        )
        for name, lines in grouped.items():
            examples: list[str] = []
            for line in lines:
                detail = line[len(name):].strip()
                label = detail or name
                if label not in examples:
                    examples.append(label)
                if len(examples) == 4:
                    break
            rendered = " · ".join(examples)
            extra = len(lines) - len(examples)
            if extra > 0:
                rendered = f"{rendered} · +{extra} more"
            console.print(
                f"    [sf.muted]· {escape(name):<10}[/] {escape(rendered)}",
                highlight=False,
            )

    # ── critic + session-pair plumbing ────────────────────────────

    def _render_critiques(
        self, event: IncomingEvent, critiques: list[Critique]
    ) -> None:
        """Print one indented suggestion per fired rule, plus the
        exact ``spec team flag`` command a reviewer would run if they
        agree with the critic. Single-quoted ``rule`` makes searching
        chat logs for a specific rule easy.

        When ``--notify`` was set on the watcher and *any* of the
        rules is ``block`` severity, we also ring the terminal bell
        and fire a best-effort macOS notification — for the case
        where the reviewer is not staring at the pane and a teammate
        just typed ``rm -rf`` or pasted a secret.
        """
        if not critiques:
            return
        block_hits: list[Critique] = []
        for c in critiques:
            console.print(
                f"  [{c.color}]{c.glyph} AUTO {c.rule:<16}[/] "
                f"[sf.muted]{c.msg}[/]"
            )
            console.print(
                f"     [sf.muted]→ {suggested_flag_command(event.id, c)}[/]"
            )
            if c.severity == SEV_HIGH:
                block_hits.append(c)
        if self._notify and block_hits:
            self._alert(event, block_hits)

    def _alert(
        self, event: IncomingEvent, hits: list[Critique]
    ) -> None:
        """Best-effort "look at the pane" alert for block-severity
        critic hits. Always rings the terminal bell (works in any
        terminal); on macOS we additionally fire ``osascript`` so the
        OS shows a banner.

        Failures are swallowed silently — alerting is a courtesy, not
        the contract of the watcher.
        """
        try:
            import sys

            # ``\a`` to stderr so it doesn't get caught by stdout
            # redirects piping the stream into a file.
            sys.stderr.write("\a")
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        try:
            osa = shutil.which("osascript")
            if not osa:
                return
            top = hits[0]
            title = f"Spec: block on {event.author_display}"
            # AppleScript quoting: escape double quotes and backslashes
            # inside the message so a stray quote in the critic text
            # doesn't break the call.
            msg = top.msg.replace("\\", "\\\\").replace('"', '\\"')
            sub_title = f"#{event.id} · {top.rule}"
            script = (
                f'display notification "{msg}" '
                f'with title "{title}" '
                f'subtitle "{sub_title}"'
            )
            subprocess.Popen(
                [osa, "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001
            pass

    def _remember_open_session(self, event: IncomingEvent) -> None:
        # Workspace stream includes your own USER rows; the no-reply hint
        # is written for teammates ("their watcher") and is noise on self.
        if self._viewer_handle and event.role == "user":
            ah = (event.author_handle or "").strip().lower()
            if ah and ah == self._viewer_handle:
                return
        key = (event.project_id, event.session_id or f"ev:{event.id}")
        # Evict oldest if the table gets too big — bounds memory at
        # the cost of losing one pairing on a freakishly busy host.
        if len(self._open_sessions) >= _OPEN_SESSIONS_MAX:
            try:
                oldest = min(self._open_sessions, key=lambda k: self._open_sessions[k][1])
                self._open_sessions.pop(oldest, None)
            except ValueError:
                pass
        ts = event.turn_at or event.received_at or datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        self._open_sessions[key] = (event.id, ts, event.author_display, False)

    def _mark_session_replied(self, event: IncomingEvent) -> None:
        key = (event.project_id, event.session_id or f"ev:{event.id}")
        self._open_sessions.pop(key, None)

    def check_open_sessions(self) -> None:
        """Surface a one-time "waiting for AI reply" hint per stale
        session. Called from the watcher's idle loop so an engineer
        notices when their teammate's agent has gone silent."""
        now = datetime.now(timezone.utc)
        threshold = timedelta(seconds=_NO_REPLY_AGE_SECS)
        to_warn: list[tuple[int, str, datetime]] = []
        with self._lock:
            for key, (ev_id, ts, author, warned) in list(
                self._open_sessions.items()
            ):
                if warned:
                    continue
                if now - ts >= threshold:
                    self._open_sessions[key] = (ev_id, ts, author, True)
                    to_warn.append((ev_id, author, ts))
        for ev_id, author, ts in to_warn:
            age = max(0, int((now - ts).total_seconds()))
            with self._lock:
                if self._live_suppress:
                    self._skipped_while_suppressed += 1
                    continue
                console.print(
                    f"  [sf.warn]⏳ no-reply[/]  "
                    f"[sf.muted]{author}'s prompt #{ev_id} is "
                    f"{age}s old with no AI reply yet — is their watcher "
                    f"sharing assistant turns?[/]"
                )

    def show_flag(self, flag: IncomingFlag) -> None:
        """Render an incoming flag frame inline with the prompt stream.

        Single line on purpose — flags are decorative annotations, not
        the main event. The glyph + role color encodes severity at a
        glance; the optional note is shown verbatim (truncated to a
        sensible width)."""
        glyph, color = _FLAG_GLYPH.get(flag.kind, ("⚑", "sf.warn"))
        author = flag.author_display
        note = (flag.note or "").strip()
        note_part = ""
        if note:
            note_short = _truncate(note, 220 if not self._compact else 100)
            note_part = f" [sf.muted]· {note_short}[/]"
        with self._lock:
            if self._live_suppress:
                self._skipped_while_suppressed += 1
                return
            console.print(
                f"  [{color}]{glyph} {flag.kind:<8}[/] "
                f"[sf.label]{author}[/] [sf.muted]· flagged #{flag.prompt_event_id}[/]"
                f"{note_part}"
            )

    def announce_heartbeat(self) -> None:
        """Visible "I am still listening" tick. Surfaced periodically
        from idle workspace watchers so engineers can tell at a glance
        that the stream is alive even when the team is quiet."""
        ts = format_live_event_clock(datetime.now(timezone.utc))
        with self._lock:
            if self._live_suppress:
                self._skipped_while_suppressed += 1
                return
            console.print(
                f"[sf.muted]· still watching · {ts}[/]"
            )

    def show_command_result(self, body: str, *, kind: str = "info") -> None:
        """Render the output of a slash-command (``/flag``, ``/summarize``,
        etc.) so it visually separates from streamed events. ``kind`` is
        one of ``info`` / ``ok`` / ``error`` / ``summarize``; each gets a
        distinct accent glyph so the eye can tell at a glance whether
        the watcher is acknowledging an action, raising an error, or
        emitting a large structured block for the agent.

        ``/summarize`` output uses Rich markup on **trusted** lines (role
        colours, separators). User-authored lines must already be
        escaped with :func:`rich.markup.escape` where they are assembled — see
        :func:`spec_cli.realtime.commands._cmd_summarize`.
        """
        glyph, color = {
            "ok": ("✓", "sf.mint"),
            "error": ("✗", "sf.reject"),
            "summarize": ("≡", "sf.point"),
            "info": ("·", "sf.point"),
        }.get(kind, ("·", "sf.point"))
        lines = body.splitlines() or [body]
        with self._lock:
            if self._live_suppress:
                self._skipped_while_suppressed += 1
                return
            console.print()
            markup_body = kind == "summarize"
            console.print(
                f"[{color}]{glyph}[/] [bold {color}]spec>[/] "
                f"{lines[0]}",
                markup=True,
                highlight=False,
            )
            for extra in lines[1:]:
                console.print(
                    f"   {extra}",
                    markup=markup_body,
                    highlight=False,
                )

    def show_in_system_pager(self, body: str, *, banner: str) -> None:
        """Pause live stream prints and show ``body`` in ``less`` / ``$PAGER``.

        Uses :func:`_resolve_system_pager_argv` (``SPEC_TEAM_WATCH_PAGER``,
        then ``PAGER``, then ``less`` / ``more``). When no pager exists,
        falls back to :meth:`show_command_result` inline. The pager buffer
        starts with a short reminder that **q** returns to the live feed.
        """
        blog = (
            f"spec team watch — thread view\n{banner.strip()}\n\n"
            "────────────────────────────────────────────────────────────────\n"
            "  Press q to leave the pager and return to the live team feed.\n"
            "────────────────────────────────────────────────────────────────\n\n"
            f"{body}"
        )
        argv = _resolve_system_pager_argv()
        with self._lock:
            self._live_suppress = True
            self._skipped_while_suppressed = 0
            console.print(
                "[sf.point]●[/] [sf.label]opening system pager[/] "
                "[sf.muted](less or PAGER env — press q when done)[/]"
            )
        try:
            if not argv:
                with self._lock:
                    self._live_suppress = False
                    self._skipped_while_suppressed = 0
                self.show_command_result(blog, kind="summarize")
                return
            env = os.environ.copy()
            env.setdefault("LESS", "-R -X")
            subprocess.run(
                argv,
                input=blog.encode("utf-8", errors="replace"),
                env=env,
            )
        except OSError as e:
            with self._lock:
                self._live_suppress = False
                self._skipped_while_suppressed = 0
            self.show_command_result(
                f"[pager failed: {e}]\n\n{blog}",
                kind="summarize",
            )
        finally:
            with self._lock:
                skipped = self._skipped_while_suppressed
                self._live_suppress = False
                self._skipped_while_suppressed = 0
                if skipped:
                    console.print(
                        f"[sf.muted]·[/] {skipped} live line(s) were not drawn while "
                        "the pager had the screen — try `[sf.label]/replay 5m[/]` "
                        "if you need them.",
                        highlight=False,
                    )

    def announce_connected(self, project_label: str) -> None:
        with self._lock:
            if self._live_suppress:
                self._skipped_while_suppressed += 1
                return
            console.print(
                f"[sf.mint]●[/] connected · [sf.label]{project_label}[/] [sf.muted]· "
                f"streaming team prompts[/]"
            )
            flush_streaming_output()

    def announce_connecting(self, detail: str) -> None:
        """Printed once before the SSE thread delivers live rows.

        Runs *after* the REST bootstrap replay so reviewers are not
        misled into thinking this was a mid-stream disconnect."""
        with self._lock:
            if self._live_suppress:
                self._skipped_while_suppressed += 1
                return
            console.print(f"[sf.warn]…[/] connecting [sf.muted]({detail})[/]")
            flush_streaming_output()

    def announce_broadcast_disabled(self) -> None:
        with self._lock:
            if self._live_suppress:
                self._skipped_while_suppressed += 1
                return
            console.print(
                "[sf.muted]·[/] receive-only mode "
                "(run [sf.label]spec live on[/] to share, or "
                "[sf.label]spec live status[/] to see why it's off)"
            )
            flush_streaming_output()

    def announce_fatal(self, msg: str) -> None:
        with self._lock:
            console.print(f"[sf.reject]✗[/] {msg}")
            flush_streaming_output()


__all__ = ["Notifier", "WORKSPACE_FEED_LABEL"]
