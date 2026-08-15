"""
Cursor adapter.

Reads Cursor's per-workspace SQLite chat store and produces
``spec_cli.prompts.Session`` objects.

Cursor stores chat data in two places:

  - **Per-workspace** ``state.vscdb`` (SQLite) under
    ``<USER_DATA>/User/workspaceStorage/<hash>/``. The sibling
    ``workspace.json`` file in that directory records the workspace
    folder as a ``file://`` URL — this is how we map *bundle path →
    storage dir(s)*. Inside ``state.vscdb`` the ``ItemTable`` row
    keyed ``composer.composerData`` lists every Composer thread that
    belongs to this workspace.

    **Important:** this is the **Composer / in-editor Agent** transcript
    model, not necessarily Cursor's separate **sidebar Chat** UI. Chats
    you open only in the right-hand Chat panel may never appear under
    ``composer.composerData`` for the workspace folder Spec resolves —
    use Agent/Composer in the editor for the same repo if you need Spec
    Live to see the thread.
  - **Global** ``state.vscdb`` under ``<USER_DATA>/User/globalStorage/``.
    Cursor stores per-thread metadata under
    ``cursorDiskKV[composerData:<composerId>]`` (which carries
    ``fullConversationHeadersOnly`` — the ordered list of bubble ids,
    plus ``modelConfig`` with ``modelName`` when bubbles omit
    ``modelInfo``)
    and per-message bodies under
    ``cursorDiskKV[bubbleId:<composerId>:<bubbleId>]``.
  - **In-editor Agent JSONL** under
    ``<USER_DATA>/projects/<encoded-workspace>/agent-transcripts/``
    (same path layout as ``codex_store_root()`` — merged here so
    ``source`` is always ``cursor``).

Why split this way? Composers are workspace-tied (you only see them in
the workspace they were created in), but the message bodies are large
and Cursor stores them once globally so opening a workspace doesn't
have to load every bubble. We mirror that split.

Scope mirrors git: a Cursor composer counts for ``bundle_root`` when
``workspace.json``'s folder *intersects* the bundle on disk. That
covers three real shapes:

  - the workspace folder is the bundle root itself,
  - the workspace folder is a subdirectory of the bundle root (you
    opened Cursor on ``<bundle>/backend``), and
  - the workspace folder is an *ancestor* of the bundle root (you
    opened Cursor on the umbrella project that contains the bundle —
    e.g. a monorepo where ``spec init`` lives in
    ``./services/billing``). Without this case Cursor users running
    a single editor over multiple bundles would never see prompts
    captured for the bundle they live inside, even though they were
    typed while looking at bundle files. This is the spirit of git's
    "any subdir of the working tree counts" rule, applied to Cursor's
    workspace abstraction.

The mapping is *not* lossy (unlike Claude Code's path encoding), so
the cwd defense-in-depth check that Claude Code needs is unnecessary
here — we just check the workspace folder URL up front.

If Cursor's on-disk format changes materially in a future release,
this is the one file to update; the prompts schema and the rest of
the CLI don't depend on Cursor internals.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from ..prompts.schema import (
    MAX_TURN_MODEL_CHARS,
    MAX_TURN_TEXT_CHARS,
    Session,
    ToolCall,
    Turn,
    validate_session,
)
from ..prompts.text_sanitize import sanitize_for_toml_text, unwrap_cursor_user_message
from ..prompts.tools import ALLOWED_TOOL_NAMES, summarize_tool_call
from .codex import CodexError, iter_cursor_agent_transcript_sessions


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


class CursorError(RuntimeError):
    """Raised for adapter-level problems (missing store, unreadable DB)."""


def _default_user_data_dir() -> Path:
    """Cursor's per-platform User data root. Honors ``CURSOR_HOME``.

    macOS:   ``~/Library/Application Support/Cursor``
    Linux:   ``~/.config/Cursor``
    Windows: ``%APPDATA%/Cursor``
    """
    override = os.environ.get("CURSOR_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path("~/Library/Application Support/Cursor").expanduser()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Cursor"
        return Path("~/AppData/Roaming/Cursor").expanduser()
    return Path("~/.config/Cursor").expanduser()


def cursor_workspace_storage_root() -> Path:
    return _default_user_data_dir() / "User" / "workspaceStorage"


def cursor_global_storage_db() -> Path:
    return _default_user_data_dir() / "User" / "globalStorage" / "state.vscdb"


def _parse_folder_uri(uri: str) -> Path | None:
    """Decode a ``file://`` workspace URI to a filesystem path.

    VS Code / Cursor write workspace folders as URI strings like
    ``file:///Users/alice/code/billing``. We percent-decode and strip
    the ``file://`` prefix; non-``file:`` schemes (remote workspaces)
    return ``None`` because Spec only knows how to read local repos.
    """
    if not isinstance(uri, str) or not uri:
        return None
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    raw_path = unquote(parsed.path)
    if not raw_path:
        return None
    # On Windows file URIs are like file:///C:/foo — strip the leading slash.
    if sys.platform == "win32" and raw_path.startswith("/") and len(raw_path) >= 3 and raw_path[2] == ":":
        raw_path = raw_path[1:]
    return Path(raw_path)


@dataclass
class _WorkspaceMatch:
    """One Cursor workspaceStorage directory we've matched to the bundle."""

    storage_dir: Path
    workspace_folder: Path  # the absolute folder path the workspace was opened at


def _workspace_dir_candidates(
    bundle_paths: Iterable[Path] | None,
) -> list[_WorkspaceMatch]:
    """Every Cursor workspaceStorage entry whose folder intersects the bundle.

    Iterates ``<storage>/*/workspace.json``, parses the recorded
    ``folder`` URL, and keeps the dir if the workspace folder
    *overlaps* any of the given bundle paths — i.e. the workspace
    folder is the bundle root, a descendant of it (Cursor opened on a
    subdir), or an ancestor of it (Cursor opened on a parent that
    contains the bundle as a child). See the module docstring for the
    full rationale; the short version is "if a developer typing in
    Cursor could plausibly have been editing this bundle, capture
    those prompts". For ancestor matches we point ``workspace_folder``
    at the bundle root rather than the (broader) Cursor workspace
    folder — the captured ``.prompts`` files belong *to the bundle*,
    not to the umbrella repo Cursor happens to be open on, and
    downstream consumers (``Session.cwd``) treat the field as "the
    folder this conversation is attributed to".

    The bundle-paths list is the rename-resilient set: the current
    bundle root plus any historical paths persisted in
    ``.spec/index.json`` (Fix #2).
    """
    storage_root = cursor_workspace_storage_root()
    if not storage_root.is_dir():
        return []

    resolved_roots = (
        None if bundle_paths is None else [p.resolve() for p in bundle_paths]
    )
    if resolved_roots == []:
        return []

    matches: list[_WorkspaceMatch] = []
    for child in sorted(storage_root.iterdir()):
        if not child.is_dir():
            continue
        manifest = child / "workspace.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        folder_uri = data.get("folder")
        folder_path = _parse_folder_uri(folder_uri) if isinstance(folder_uri, str) else None
        if folder_path is None:
            # Multi-root workspaces use ``configuration`` instead — skip
            # those for v0.1; Spec bundles map to a single folder anyway.
            continue
        try:
            resolved_folder = folder_path.resolve()
        except OSError:
            continue

        # Match shape against every recorded bundle path. We care
        # about the FIRST match because that's the canonical bundle
        # location to attribute the session to:
        #   - exact / descendant: workspace_folder is the natural cwd
        #   - ancestor: anchor the session at the bundle root, not at
        #     the wider Cursor workspace folder. The folder we record
        #     becomes ``Session.cwd`` and is what shows up in `.prompts`
        #     metadata, so it has to read as "this conversation is
        #     about <bundle>", not "this conversation lived in some
        #     parent monorepo".
        anchor: Path | None = resolved_folder if resolved_roots is None else None
        for r in resolved_roots or []:
            if resolved_folder == r or r in resolved_folder.parents:
                anchor = resolved_folder
                break
            if resolved_folder in r.parents:
                anchor = r
                break
        if anchor is None:
            continue
        matches.append(
            _WorkspaceMatch(
                storage_dir=child,
                workspace_folder=anchor,
            )
        )
    return matches


# ---------------------------------------------------------------------------
# SQLite reads
# ---------------------------------------------------------------------------


def _read_item_table(db_path: Path, key: str) -> Any | None:
    """Return ``json.loads(value)`` for a row in ``ItemTable``, or ``None``.

    Read-only opens. Cursor may have the DB open with WAL writes in
    flight; SQLite handles concurrent readers fine, but we don't take
    locks or modify anything.
    """
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key = ?", (key,)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row:
        return None
    raw = row[0]
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _read_disk_kv_from_connection(
    conn: sqlite3.Connection, key: str
) -> Any | None:
    """Read one decoded ``cursorDiskKV`` value from an existing connection."""
    try:
        row = conn.execute(
            "SELECT value FROM cursorDiskKV WHERE key = ?", (key,)
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    raw = row[0]
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _read_disk_kv(db_path: Path, key: str) -> Any | None:
    """Return ``json.loads(value)`` for a row in ``cursorDiskKV``, or ``None``."""
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        return _read_disk_kv_from_connection(conn, key)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bubble → Turn conversion
# ---------------------------------------------------------------------------


# Cursor encodes bubble role as an integer in the ``type`` field. ``1`` is
# the user, ``2`` is the assistant. Other values (system / tool-result-like
# bubbles) are dropped — the prompt schema only models user + assistant.
_BUBBLE_TYPE_USER = 1
_BUBBLE_TYPE_ASSISTANT = 2

# Same cap the Claude Code adapter uses for assistant summaries — keeps
# captured `.prompts` files visually consistent across sources.
_SUMMARY_CHARS: int = 200


# Cursor's internal tool names → canonical spec tool name. Cursor adds
# version suffixes (``_v2``) and uses its own verbs (``ripgrep_raw_search``,
# ``run_terminal_command``) where Claude Code / Codex would say ``Grep``
# or ``Bash``. We normalize on the wire so receivers can apply uniform
# rendering / critic logic regardless of which agent produced the call.
_CURSOR_TOOL_NAME_MAP: dict[str, str] = {
    "read_file": "Read",
    "read_file_v2": "Read",
    "read_files": "Read",
    "ripgrep_raw_search": "Grep",
    "grep_search": "Grep",
    "codebase_search": "Grep",
    "glob_file_search": "Glob",
    "file_search": "Glob",
    "run_terminal_cmd": "Bash",
    "run_terminal_command": "Bash",
    "run_terminal_command_v2": "Bash",
    "edit_file": "Edit",
    "search_replace": "Edit",
    "multi_edit_file": "Edit",
    "write_file": "Write",
    "create_file": "Write",
    "delete_file": "Delete",
    "web_search": "WebSearch",
    "web_fetch": "WebFetch",
    "fetch_url": "WebFetch",
    "todo_write": "TodoWrite",
    "list_dir": "Glob",
}

# Bubble fields that indicate Cursor recorded a real user turn even
# when ``text`` / ``richText`` are empty. Same idea as the assistant
# probe below — bias toward emitting a placeholder rather than
# silently dropping the row.
_CURSOR_USER_ACTIVITY_KEYS = (
    "attachments",
    "imageData",
    "commands",
    "mentionData",
    "fileSelections",
    "selectionData",
    "voiceInputData",
)


def _bubble_has_user_activity(bubble: dict[str, Any]) -> bool:
    """True when a user bubble has structured content but no prose.

    We use an explicit allowlist of known activity-signalling fields
    instead of "any field beyond bare metadata" — Cursor stores a
    surprising amount of bookkeeping per bubble (``_v`` schema
    version, ``revisionId``, etc.) and a denylist would emit
    placeholders for legitimately empty bubbles.
    """
    for key in _CURSOR_USER_ACTIVITY_KEYS:
        if bubble.get(key):
            return True
    return False


def _extract_cursor_tool_call(bubble: dict[str, Any]) -> ToolCall | None:
    """Extract a single ``ToolCall`` from a Cursor assistant bubble's
    ``toolFormerData`` blob, or ``None`` when the bubble doesn't carry one.

    Cursor stores tool invocations as separate type=2 bubbles whose body
    is a ``toolFormerData`` dict — ``name`` (Cursor's internal tool id),
    ``rawArgs`` (JSON-encoded arguments, sometimes empty), ``params``
    (also JSON, more complete for some tools), and ``status``.

    We map Cursor's tool name to the spec canonical name (so ``Edit``,
    ``Bash``, ``Read``, etc. mean the same thing whether the agent was
    Cursor, Claude Code, or Codex), pick the most populated arg source,
    then route the args through ``summarize_tool_call`` so we get the
    same scrubbed, bounded shape every other adapter ships. Tools that
    fall outside the allowlist (e.g. random MCP servers) are dropped.
    """
    tf = bubble.get("toolFormerData")
    if not isinstance(tf, dict):
        return None
    raw_name = tf.get("name")
    if not isinstance(raw_name, str) or not raw_name:
        return None
    canonical = _CURSOR_TOOL_NAME_MAP.get(raw_name)
    if canonical is None or canonical not in ALLOWED_TOOL_NAMES:
        return None

    # Cursor's ``params`` is often more complete than ``rawArgs``
    # (e.g. terminal commands populate ``params`` but leave
    # ``rawArgs`` as an empty string). Try both.
    args: dict[str, Any] = {}
    for source_key in ("rawArgs", "params"):
        raw = tf.get(source_key)
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                args = parsed
                # Bash carries the real command under ``params.command`` even
                # when ``rawArgs.command`` is set, so the deeper key wins.
                if args.get("command"):
                    break
        elif isinstance(raw, dict):
            args = raw
            break

    # Cursor wraps the command for Bash inside ``params.command`` and
    # nests file edits / writes in different shapes. Normalize the
    # handful of forms ``summarize_tool_call`` doesn't already handle.
    if canonical == "Bash" and not args.get("command"):
        params_str = tf.get("params")
        if isinstance(params_str, str) and params_str.strip():
            try:
                parsed = json.loads(params_str)
                if isinstance(parsed, dict) and parsed.get("command"):
                    args = {**args, "command": parsed["command"]}
            except json.JSONDecodeError:
                pass

    summarized = summarize_tool_call(canonical, args)
    if summarized is None:
        return None

    status = tf.get("status") if isinstance(tf.get("status"), str) else None
    return ToolCall(name=canonical, args=summarized, status=status)


def _first_sentence(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    for terminator in (". ", "? ", "! ", "\n"):
        idx = stripped.find(terminator)
        if 0 < idx < _SUMMARY_CHARS:
            return stripped[: idx + 1].rstrip()
    if len(stripped) <= _SUMMARY_CHARS:
        return stripped
    return stripped[:_SUMMARY_CHARS].rstrip() + "…"


def _preview(text: str) -> str:
    """Truncate to the schema max turn size, adding a marker when we cut.

    Uses :data:`MAX_TURN_TEXT_CHARS` so Spec Live and ``.prompts`` capture
    can ship long assistant replies (large markdown code blocks, pasted
    logs) without an extra 48 KiB adapter-specific ceiling. The watcher
    still applies its own outbound cap before POST."""
    stripped = text.strip()
    if len(stripped) <= MAX_TURN_TEXT_CHARS:
        return stripped
    # Try to break at a paragraph or newline boundary so the preview
    # ends at a natural reading point, not mid-sentence.
    marker = "\n\n[…truncated…]"
    limit = MAX_TURN_TEXT_CHARS - len(marker)
    cut = stripped.rfind("\n\n", 0, limit)
    if cut < limit // 2:
        cut = stripped.rfind("\n", 0, limit)
    if cut < limit // 2:
        cut = limit
    return stripped[:cut].rstrip() + marker


def _parse_bubble_timestamp(raw: Any) -> datetime | None:
    """Cursor's ``createdAt`` is sometimes ISO 8601 and sometimes ms-epoch."""
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
                timezone.utc
            )
        except ValueError:
            return None
    if isinstance(raw, (int, float)) and raw > 0:
        # ms since epoch (Cursor) vs s since epoch (older blobs). Anything
        # past ~year 33658 in seconds becomes >1e12, which is the threshold
        # below which we treat the value as seconds.
        seconds = raw / 1000.0 if raw > 1e12 else float(raw)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    return None


def _bubble_text(bubble: dict[str, Any]) -> str:
    """Extract the prose body from a Cursor bubble.

    Bubbles store both a Lexical-formatted ``richText`` blob and a flat
    ``text`` field. We prefer ``text`` when it is non-empty; some Cursor
    builds only populate ``richText`` for user bubbles, and skipping those
    dropped prompts from Spec Live entirely.
    """
    text = bubble.get("text")
    if isinstance(text, str):
        out = sanitize_for_toml_text(text)
        if out.strip():
            return out
    rich = bubble.get("richText")
    if rich is not None:
        lexical = _lexical_plain_text(rich)
        if lexical.strip():
            return sanitize_for_toml_text(lexical)
    return ""


def _lexical_plain_text(raw: Any) -> str:
    """Best-effort plain text from Cursor's Lexical ``richText`` field.

    Accepts JSON string or already-parsed dict/list. Walks Lexical's
    ``children`` tree and concatenates ``type: \"text\"`` node bodies.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return ""
        try:
            parsed: Any = json.loads(s)
        except json.JSONDecodeError:
            return s
    else:
        parsed = raw

    parts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text":
                t = node.get("text")
                if isinstance(t, str) and t.strip():
                    parts.append(t)
            ch = node.get("children")
            if isinstance(ch, list):
                for c in ch:
                    walk(c)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    if isinstance(parsed, dict) and isinstance(parsed.get("root"), dict):
        walk(parsed["root"])
    else:
        walk(parsed)
    return "\n".join(parts) if parts else ""


def _cursor_composer_default_model(composer_data: dict[str, Any]) -> str | None:
    """Thread-level model when assistant bubbles omit ``modelInfo``.

    Live Cursor stores often put the active model only on
    ``composerData.modelConfig`` (``modelName`` / ``model``) while many
    assistant ``bubbleId:*`` payloads have no ``modelInfo`` at all.
    We still attach that default to each assistant turn so capture,
    compiler routing, and ``spec status`` stay truthful.
    """
    mc = composer_data.get("modelConfig")
    if not isinstance(mc, dict):
        return None
    raw = mc.get("modelName") or mc.get("model")
    if not isinstance(raw, str):
        return None
    m = raw.strip()
    if not m:
        return None
    return m[:MAX_TURN_MODEL_CHARS]


def _ms_epoch_to_utc(ms: Any) -> datetime | None:
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


# ---------------------------------------------------------------------------
# Session assembly
# ---------------------------------------------------------------------------


@dataclass
class _SessionBuilder:
    """Mutable accumulator for a single Cursor composer."""

    id: str
    source: str = "cursor"
    cwd: str | None = None
    model: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    turns: list[Turn] = field(default_factory=list)
    paths_touched: list[str] = field(default_factory=list)

    def to_session(self, *, verbose: bool, name: str | None) -> Session | None:
        if not self.turns:
            return None
        # The schema requires `verbose=True` whenever any assistant turn
        # carries `text`. Even with `verbose=False` from the caller, an
        # assistant turn could have `text` if a future code path adds
        # one — guard that case so the rendered file always parses.
        marker = verbose or any(
            t.role == "assistant" and t.text for t in self.turns
        )
        return Session(
            id=self.id,
            source=self.source,
            turns=self.turns,
            started_at=self.started_at,
            ended_at=self.ended_at,
            cwd=self.cwd,
            model=self.model,
            title=name or None,
            verbose=marker,
            paths_touched=list(self.paths_touched),
        )


def _build_session(
    composer_id: str,
    composer_data: dict[str, Any],
    workspace_folder: Path,
    *,
    global_conn: sqlite3.Connection,
    verbose: bool,
) -> Session | None:
    """Stitch one Cursor composer into a Session, coalescing the many
    bubbles Cursor stores per assistant reply into a single logical
    :class:`Turn`.

    Cursor splits one model reply across many type-2 bubbles: prose
    chunks, thinking-only bubbles (no text), and one ``toolFormerData``
    bubble per tool invocation. Treating each bubble as its own turn —
    which earlier versions of this adapter did — exploded a single
    agent run into 30-70 noisy events in ``spec team watch`` and lost
    the prose tail entirely whenever it lived in a different bubble
    from the intro.

    The new shape:

    * ``type == 1``  → one user :class:`Turn` (prose body).
    * Run of consecutive ``type == 2`` bubbles between two user bubbles
      → ONE assistant :class:`Turn` whose ``text`` is the joined prose
      and ``tool_calls`` is the ordered list of normalized tool
      invocations seen in that run.

    Tool calls are extracted from ``toolFormerData`` via the spec
    tool allowlist (``Read``, ``Edit``, ``Bash``, etc.). Cursor's
    internal tool names (``ripgrep_raw_search``, ``run_terminal_command_v2``)
    are mapped to the canonical names so receivers can apply uniform
    rendering / critic logic regardless of which agent produced the
    call.
    """
    headers = composer_data.get("fullConversationHeadersOnly")
    if not isinstance(headers, list):
        return None

    builder = _SessionBuilder(id=composer_id)
    builder.cwd = str(workspace_folder)

    name = composer_data.get("name")
    if not isinstance(name, str):
        name = None

    builder.started_at = _ms_epoch_to_utc(composer_data.get("createdAt"))
    builder.ended_at = _ms_epoch_to_utc(composer_data.get("lastUpdatedAt"))
    default_model = _cursor_composer_default_model(composer_data)

    # Accumulator for the current run of consecutive assistant bubbles.
    # Flushed into one :class:`Turn` on the next user bubble or end of
    # session. ``texts`` holds prose-bubble bodies (later joined with
    # blank lines so the rendered prose reads as paragraphs);
    # ``tool_calls`` holds extracted ``ToolCall``s in bubble order so a
    # reviewer can replay what the agent actually did.
    pending_texts: list[str] = []
    pending_tool_calls: list[ToolCall] = []
    pending_paths: list[str] = []
    pending_first_at: datetime | None = None
    pending_last_at: datetime | None = None
    pending_model: str | None = None
    pending_has_activity: bool = False

    def _flush_assistant() -> None:
        nonlocal pending_texts, pending_tool_calls, pending_paths
        nonlocal pending_first_at, pending_last_at
        nonlocal pending_model, pending_has_activity
        if not pending_has_activity:
            return
        joined = "\n\n".join(t.strip() for t in pending_texts if t.strip())
        joined = joined.strip()
        summary: str | None = None
        if joined:
            summary = _first_sentence(joined)
        if not summary and not joined and not pending_tool_calls:
            # Truly empty run (thinking bubbles only, no tools, no prose) —
            # drop. Without a summary we can't honour the schema's
            # "assistant must carry text or summary" contract.
            pending_texts = []
            pending_tool_calls = []
            pending_paths = []
            pending_first_at = None
            pending_last_at = None
            pending_model = None
            pending_has_activity = False
            return
        preview_text = _preview(joined) if (verbose and joined) else None
        # When prose is unavailable, leave summary empty here — the
        # broadcaster synthesizes a ``ran N tools: …`` line from
        # ``tool_calls`` later in the pipeline, which gives reviewers a
        # consistent shape across every source adapter.
        builder.turns.append(
            Turn(
                role="assistant",
                summary=summary or None,
                text=preview_text,
                at=pending_last_at or pending_first_at,
                model=pending_model,
                tool_calls=list(pending_tool_calls),
            )
        )
        if builder.model is None and pending_model:
            builder.model = pending_model
        # Track files the run touched so receivers and ``spec status``
        # can show "touched X, Y" chips without needing to walk
        # tool_calls.
        for p in pending_paths:
            if p and p not in builder.paths_touched:
                builder.paths_touched.append(p)
        pending_texts = []
        pending_tool_calls = []
        pending_paths = []
        pending_first_at = None
        pending_last_at = None
        pending_model = None
        pending_has_activity = False

    for header in headers:
        if not isinstance(header, dict):
            continue
        bubble_id = header.get("bubbleId")
        if not isinstance(bubble_id, str) or not bubble_id:
            continue
        bubble = _read_disk_kv_from_connection(
            global_conn, f"bubbleId:{composer_id}:{bubble_id}"
        )
        if not isinstance(bubble, dict):
            continue
        btype = bubble.get("type") if isinstance(bubble.get("type"), int) else None

        at = _parse_bubble_timestamp(bubble.get("createdAt"))

        if btype == _BUBBLE_TYPE_USER:
            # A user turn always closes any pending assistant run that
            # belonged to the *previous* user prompt.
            _flush_assistant()
            text = unwrap_cursor_user_message(_bubble_text(bubble))
            if not text.strip():
                if _bubble_has_user_activity(bubble):
                    text = "(prompt body not extractable — see Cursor)"
                else:
                    continue
            builder.turns.append(Turn(role="user", text=_preview(text), at=at))
            continue

        if btype != _BUBBLE_TYPE_ASSISTANT:
            # System / status bubbles — drop wholesale.
            continue

        # ── consecutive assistant bubble → accumulate into pending run ──
        bubble_model: str | None = None
        model_info = bubble.get("modelInfo")
        if isinstance(model_info, dict):
            m = model_info.get("modelName") or model_info.get("model")
            if isinstance(m, str) and m.strip():
                bubble_model = m.strip()[:MAX_TURN_MODEL_CHARS]
        if bubble_model is None and default_model:
            bubble_model = default_model
        if pending_model is None and bubble_model is not None:
            pending_model = bubble_model

        if pending_first_at is None:
            pending_first_at = at
        if at is not None:
            pending_last_at = at

        # Pull tool call (if any) before checking prose so a tool-only
        # bubble still counts as activity.
        call = _extract_cursor_tool_call(bubble)
        if call is not None:
            pending_tool_calls.append(call)
            pending_has_activity = True
            p = call.args.get("path") if isinstance(call.args, dict) else None
            if isinstance(p, str) and p:
                # Strip workspace prefix so the chip stays readable —
                # ``billing.py`` is more useful than the absolute path.
                short = p
                try:
                    rel = Path(p).resolve().relative_to(workspace_folder)
                    short = str(rel)
                except (ValueError, OSError):
                    short = Path(p).name
                pending_paths.append(short)

        text = _bubble_text(bubble)
        if text.strip():
            pending_texts.append(text)
            pending_has_activity = True

    # Close the trailing assistant run, if any.
    _flush_assistant()

    session = builder.to_session(verbose=verbose, name=name)
    if session is None:
        return None
    # Defensive re-validation — a future schema constraint should
    # surface as an adapter bug, not silent corruption of a .prompts
    # file at write time.
    validate_session(session)
    return session


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _iter_composer_sessions(
    matches: Iterable[_WorkspaceMatch],
    *,
    global_db: Path,
    since: datetime | None,
    verbose: bool,
    yielded: set[str],
) -> Iterable[Session]:
    """Read matched Composer sessions through one SQLite connection.

    A machine-wide scan can touch thousands of bubbles. Reopening the global
    database for every bubble made a single poll take over a minute; one
    read-only connection keeps the same query/parser boundary without the
    connection churn.
    """
    try:
        uri = f"file:{global_db}?mode=ro"
        global_conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error:
        return  # type: ignore[return-value]
    try:
        for match in matches:
            workspace_db = match.storage_dir / "state.vscdb"
            if not workspace_db.is_file():
                continue

            composer_index = _read_item_table(workspace_db, "composer.composerData")
            if not isinstance(composer_index, dict):
                continue

            all_composers = composer_index.get("allComposers")
            if not isinstance(all_composers, list):
                continue

            # Composer order is stable: oldest first by ``createdAt``. Sort
            # explicitly so output is deterministic across runs and machines.
            composer_entries: list[tuple[int, str]] = []
            for entry in all_composers:
                if not isinstance(entry, dict):
                    continue
                cid = entry.get("composerId")
                if not isinstance(cid, str) or not cid:
                    continue
                activity = entry.get("lastUpdatedAt") or entry.get("createdAt")
                activity_at = _parse_bubble_timestamp(activity)
                if since is not None and activity_at is not None:
                    if activity_at < since:
                        continue
                ts = activity if isinstance(activity, (int, float)) else 0
                composer_entries.append((int(ts), cid))
            composer_entries.sort()

            for _, composer_id in composer_entries:
                if composer_id in yielded:
                    continue
                data = _read_disk_kv_from_connection(
                    global_conn, f"composerData:{composer_id}"
                )
                if not isinstance(data, dict):
                    continue
                try:
                    session = _build_session(
                        composer_id,
                        data,
                        workspace_folder=match.workspace_folder,
                        global_conn=global_conn,
                        verbose=verbose,
                    )
                except Exception as e:  # noqa: BLE001
                    raise CursorError(
                        f"composer {composer_id}: could not build session — {e}"
                    ) from e
                if session is None:
                    continue
                activity_at = session.ended_at or session.started_at
                if since is not None and activity_at is not None:
                    if activity_at < since:
                        continue
                yielded.add(composer_id)
                yield session
    finally:
        global_conn.close()


def read_cursor_sessions(
    bundle_paths: Path | Iterable[Path] | None,
    *,
    since: datetime | None = None,
    verbose: bool = False,
) -> Iterable[Session]:
    """Yield Cursor sessions for a bundle, or every session when scope is ``None``.

    Accepts either a single bundle root or an iterable of roots —
    current location plus every historical path the bundle has lived
    at. Pass the iterable form when you want rename-resilient
    discovery; the typical caller is ``prompts capture``, which routes
    through ``stage.historical_bundle_paths`` so a moved bundle still
    finds its old sessions (Fix #2).

    Returns **Composer** sessions from workspace SQLite plus **Agent
    JSONL** sessions from ``.../agent-transcripts/`` when present.

    Scope mirrors git: a composer counts if Cursor was opened on the
    bundle root or any subdirectory of it. We map bundle paths to
    Cursor workspaceStorage entries via each entry's
    ``workspace.json``; this mapping is exact (URLs decode losslessly),
    so unlike the Claude Code adapter we don't need a cwd cross-check
    on every bubble.

    ``since`` filters by the composer's latest activity. Composers with the
    same id across multiple workspaces (rare — would require manual
    UUID re-use) are de-duplicated within a single read.
    """
    roots: list[Path] | None = (
        None
        if bundle_paths is None
        else [bundle_paths]
        if isinstance(bundle_paths, Path)
        else list(bundle_paths)
    )
    if roots == []:
        return  # type: ignore[return-value]

    yielded: set[str] = set()

    try:
        for session in iter_cursor_agent_transcript_sessions(
            roots, since=since, verbose=verbose
        ):
            if session.id in yielded:
                continue
            yielded.add(session.id)
            yield session
    except CodexError as e:
        raise CursorError(f"cursor agent transcripts: {e}") from e

    matches = _workspace_dir_candidates(roots)
    if not matches:
        return  # type: ignore[return-value]

    global_db = cursor_global_storage_db()
    if not global_db.is_file():
        # No global storage means no composer bubble bodies; agent JSONL
        # may still have been yielded above.
        return  # type: ignore[return-value]

    yield from _iter_composer_sessions(
        matches,
        global_db=global_db,
        since=since,
        verbose=verbose,
        yielded=yielded,
    )
