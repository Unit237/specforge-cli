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
  - **Global** ``state.vscdb`` under ``<USER_DATA>/User/globalStorage/``.
    Cursor stores per-thread metadata under
    ``cursorDiskKV[composerData:<composerId>]`` (which carries
    ``fullConversationHeadersOnly`` — the ordered list of bubble ids)
    and per-message bodies under
    ``cursorDiskKV[bubbleId:<composerId>:<bubbleId>]``.

Why split this way? Composers are workspace-tied (you only see them in
the workspace they were created in), but the message bodies are large
and Cursor stores them once globally so opening a workspace doesn't
have to load every bubble. We mirror that split.

Scope mirrors git: a Cursor composer counts for ``bundle_root`` if
``workspace.json`` resolves to that bundle root or a descendant of it.
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

from ..prompts.schema import Session, Turn, validate_session
from ..prompts.text_sanitize import sanitize_for_toml_text


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
    bundle_paths: Iterable[Path],
) -> list[_WorkspaceMatch]:
    """Every Cursor workspaceStorage entry whose folder is inside the bundle.

    Iterates ``<storage>/*/workspace.json``, parses the recorded
    ``folder`` URL, and keeps the dir if the folder equals or is a
    descendant of any of the given bundle paths.

    The bundle-paths list is the rename-resilient set: the current
    bundle root plus any historical paths persisted in
    ``.spec/index.json`` (Fix #2). Each match remembers which folder
    URL got it in so downstream code can build the ``Session.cwd``
    that the rest of the pipeline expects.
    """
    storage_root = cursor_workspace_storage_root()
    if not storage_root.is_dir():
        return []

    resolved_roots = [p.resolve() for p in bundle_paths]
    if not resolved_roots:
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
        if not any(
            resolved_folder == r or r in resolved_folder.parents
            for r in resolved_roots
        ):
            continue
        matches.append(
            _WorkspaceMatch(
                storage_dir=child,
                workspace_folder=resolved_folder,
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


def _read_disk_kv(db_path: Path, key: str) -> Any | None:
    """Return ``json.loads(value)`` for a row in ``cursorDiskKV``, or ``None``."""
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM cursorDiskKV WHERE key = ?", (key,)
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

# Default cap on the assistant `text` *preview* when `verbose=True`. The
# whole point of capturing `text` (vs. `summary` only) is to give a
# reviewer enough context to evaluate what the agent actually said, not
# to mirror the full transcript. 4000 chars is roughly the first 600
# words — long enough to read the AI's reasoning, short enough that
# `.prompts` files stay diff-friendly. The schema's hard cap
# (`MAX_TURN_TEXT_CHARS = 512 KiB`) still applies.
_PREVIEW_CHARS: int = 4000


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
    """Truncate to the preview window, adding an ellipsis when we cut."""
    stripped = text.strip()
    if len(stripped) <= _PREVIEW_CHARS:
        return stripped
    # Try to break at a paragraph or newline boundary so the preview
    # ends at a natural reading point, not mid-sentence.
    cut = stripped.rfind("\n\n", 0, _PREVIEW_CHARS)
    if cut < _PREVIEW_CHARS // 2:
        cut = stripped.rfind("\n", 0, _PREVIEW_CHARS)
    if cut < _PREVIEW_CHARS // 2:
        cut = _PREVIEW_CHARS
    return stripped[:cut].rstrip() + "\n\n[…truncated…]"


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
    ``text`` field. We use ``text`` exclusively — it's already
    plaintext and matches what the user actually typed / saw.
    """
    text = bubble.get("text")
    if not isinstance(text, str):
        return ""
    return sanitize_for_toml_text(text)


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
        )


def _build_session(
    composer_id: str,
    composer_data: dict[str, Any],
    workspace_folder: Path,
    *,
    global_db: Path,
    verbose: bool,
) -> Session | None:
    """Stitch one Cursor composer into a Session.

    Reads the composer's bubble-id list out of ``fullConversationHeadersOnly``
    and pulls each bubble body from the global DB in order. Bubbles
    that produce no usable turn (empty user text, empty assistant
    summary with no tool calls) are dropped; if every bubble drops
    out, the composer yields no Session.
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

    for header in headers:
        if not isinstance(header, dict):
            continue
        bubble_id = header.get("bubbleId")
        if not isinstance(bubble_id, str) or not bubble_id:
            continue
        bubble = _read_disk_kv(global_db, f"bubbleId:{composer_id}:{bubble_id}")
        if not isinstance(bubble, dict):
            continue
        btype = bubble.get("type") if isinstance(bubble.get("type"), int) else None

        # Cursor stamps a per-bubble timestamp under createdAt; fall back
        # to the header type's ms if absent. Either way we always pick
        # something so .at is never None for valid bubbles.
        at = _parse_bubble_timestamp(bubble.get("createdAt"))

        if btype == _BUBBLE_TYPE_USER:
            text = _bubble_text(bubble)
            if not text.strip():
                continue
            builder.turns.append(Turn(role="user", text=text, at=at))
        elif btype == _BUBBLE_TYPE_ASSISTANT:
            text = _bubble_text(bubble)
            summary = _first_sentence(text)
            # If the bubble had no human-readable text and we don't yet
            # extract Cursor's tool calls (deferred — see module-level
            # docstring), there's nothing meaningful to record.
            if not summary and not (verbose and text):
                continue
            preview_text = _preview(text) if (verbose and text) else None
            builder.turns.append(
                Turn(
                    role="assistant",
                    summary=summary or None,
                    text=preview_text,
                    at=at,
                )
            )
            # Surface Cursor's chosen model when the bubble exposes it.
            model_info = bubble.get("modelInfo")
            if isinstance(model_info, dict):
                m = model_info.get("modelName") or model_info.get("model")
                if isinstance(m, str) and m and builder.model is None:
                    builder.model = m
        # Other bubble types (system / status) are dropped wholesale.

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


def read_cursor_sessions(
    bundle_paths: Path | Iterable[Path],
    *,
    since: datetime | None = None,
    verbose: bool = False,
) -> Iterable[Session]:
    """Yield every Cursor Composer session captured for the given bundle.

    Accepts either a single bundle root or an iterable of roots —
    current location plus every historical path the bundle has lived
    at. Pass the iterable form when you want rename-resilient
    discovery; the typical caller is ``prompts capture``, which routes
    through ``stage.historical_bundle_paths`` so a moved bundle still
    finds its old sessions (Fix #2).

    Scope mirrors git: a composer counts if Cursor was opened on the
    bundle root or any subdirectory of it. We map bundle paths to
    Cursor workspaceStorage entries via each entry's
    ``workspace.json``; this mapping is exact (URLs decode losslessly),
    so unlike the Claude Code adapter we don't need a cwd cross-check
    on every bubble.

    ``since`` filters by composer ``createdAt``. Composers with the
    same id across multiple workspaces (rare — would require manual
    UUID re-use) are de-duplicated within a single read.
    """
    roots: list[Path] = (
        [bundle_paths]
        if isinstance(bundle_paths, Path)
        else list(bundle_paths)
    )
    if not roots:
        return  # type: ignore[return-value]

    matches = _workspace_dir_candidates(roots)
    if not matches:
        return  # type: ignore[return-value]

    global_db = cursor_global_storage_db()
    if not global_db.is_file():
        # No global storage means no bubble bodies to read; yield nothing.
        return  # type: ignore[return-value]

    yielded: set[str] = set()

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
            created = entry.get("createdAt")
            ts = created if isinstance(created, (int, float)) else 0
            composer_entries.append((int(ts), cid))
        composer_entries.sort()

        for _, composer_id in composer_entries:
            if composer_id in yielded:
                continue
            data = _read_disk_kv(global_db, f"composerData:{composer_id}")
            if not isinstance(data, dict):
                continue
            try:
                session = _build_session(
                    composer_id,
                    data,
                    workspace_folder=match.workspace_folder,
                    global_db=global_db,
                    verbose=verbose,
                )
            except Exception as e:  # noqa: BLE001
                raise CursorError(
                    f"composer {composer_id}: could not build session — {e}"
                ) from e
            if session is None:
                continue
            if since is not None and session.started_at is not None:
                if session.started_at < since:
                    continue
            yielded.add(composer_id)
            yield session
