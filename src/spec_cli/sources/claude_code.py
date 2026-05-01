"""
Claude Code adapter.

Reads `~/.claude/projects/<encoded-cwd>/*.jsonl` for a given bundle root and
produces `spec_cli.prompts.Session` objects. Every tool call is
sanitized through the shared allowlist/summarizer in `prompts.tools`; file
contents, command output, and payloads are never stored.

What we rely on in Claude Code's on-disk format (observed, not promised):

  - One JSONL file per session, filename = `<sessionId>.jsonl`.
  - The containing directory's name encodes the session's `cwd` by
    replacing `/` with `-` and keeping a leading `-` for the root slash.
  - Every row is a JSON object with at least `type`; message rows also
    carry `timestamp`, `sessionId`, and a `message` object.
  - User message content is a string OR a list of blocks (tool_result
    blocks are skipped).
  - Assistant message content is a list of `{type: "text"|"tool_use", …}`
    blocks.

If this format changes materially in a Claude Code release, this is the
one file to update.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..prompts.schema import MAX_TURN_MODEL_CHARS, Session, ToolCall, Turn, validate_session
from ..prompts.text_sanitize import sanitize_for_toml_text
from ..prompts.tools import ALLOWED_TOOL_NAMES, summarize_tool_call

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_DEFAULT_CLAUDE_HOME = "~/.claude"


class ClaudeCodeError(RuntimeError):
    """Raised for adapter-level problems (missing store, unreadable JSONL)."""


def claude_code_store_root() -> Path:
    """Location of Claude Code's per-project session store.

    Honors `CLAUDE_HOME` as an override so tests and weird installs can
    point somewhere else.
    """
    override = os.environ.get("CLAUDE_HOME")
    base = Path(override).expanduser() if override else Path(_DEFAULT_CLAUDE_HOME).expanduser()
    return base / "projects"


def encode_bundle_path(bundle_root: Path) -> str:
    """Translate a bundle root to the directory name Claude Code uses.

    Claude Code encodes `/Users/alice/code/billing` as
    `-Users-alice-code-billing` — i.e. every `/` becomes `-`, and the
    leading slash becomes a leading `-`. We mirror that exactly.
    """
    resolved = bundle_root.resolve()
    # resolved.as_posix() is absolute on the platforms Claude Code runs on
    # (macOS, Linux). Windows-style paths would need a separate mapping,
    # but Claude Code itself doesn't target Windows today.
    return resolved.as_posix().replace("/", "-")


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------


@dataclass
class _RawEntry:
    """One parsed JSONL line, typed just enough to dispatch on."""

    type: str
    data: dict[str, Any]
    timestamp: datetime | None
    is_sidechain: bool


def _parse_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        # Python's fromisoformat handles +00:00 since 3.7; `Z` suffix needs
        # translation on <3.11.
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        return None


def _iter_jsonl(path: Path) -> Iterator[_RawEntry]:
    """Yield each row as a _RawEntry. Silently skips blank lines and
    syntactically broken rows — a partially-corrupt session file should
    not crash the whole sync."""
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            t = obj.get("type")
            if not isinstance(t, str):
                continue
            yield _RawEntry(
                type=t,
                data=obj,
                timestamp=_parse_timestamp(obj.get("timestamp")),
                is_sidechain=bool(obj.get("isSidechain")),
            )


# ---------------------------------------------------------------------------
# Content → Turn conversion
# ---------------------------------------------------------------------------

# Cap on the summary field length for captured assistant turns. Matches the
# "short bounded text" contract in docs/prompt-format.md.
_SUMMARY_CHARS: int = 200

# Default preview cap for `text` when `verbose=True`. See cursor.py for
# the rationale — same number on both adapters so captured files read
# consistently regardless of which agent produced them.
_PREVIEW_CHARS: int = 4000


def _first_sentence(text: str) -> str:
    """Extract a sentence-ish prefix for the `summary` field."""
    stripped = text.strip()
    if not stripped:
        return ""
    # Cheap sentence splitter — good enough for summaries. We don't need
    # linguistic perfection, just something that reads naturally.
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
    cut = stripped.rfind("\n\n", 0, _PREVIEW_CHARS)
    if cut < _PREVIEW_CHARS // 2:
        cut = stripped.rfind("\n", 0, _PREVIEW_CHARS)
    if cut < _PREVIEW_CHARS // 2:
        cut = _PREVIEW_CHARS
    return stripped[:cut].rstrip() + "\n\n[…truncated…]"


def _extract_user_text(content: Any) -> str | None:
    """Pull the text from a Claude Code `user` message.

    Returns None if the message is purely a tool_result (not a real user
    turn in our model).
    """
    if isinstance(content, str):
        out = sanitize_for_toml_text(content)
        return out if out.strip() else None
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(sanitize_for_toml_text(text))
            # tool_result blocks and anything else: dropped.
        joined = "\n\n".join(p for p in parts if p.strip())
        return joined or None
    return None


def _extract_assistant_pieces(content: Any) -> tuple[str, list[ToolCall]]:
    """Pull `(summary_text, tool_calls)` from a Claude Code `assistant`
    message."""
    texts: list[str] = []
    calls: list[ToolCall] = []
    if not isinstance(content, list):
        return "", []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str):
                texts.append(sanitize_for_toml_text(text))
        elif btype == "tool_use":
            name = block.get("name")
            if not isinstance(name, str) or not name:
                continue
            if name not in ALLOWED_TOOL_NAMES:
                # Out-of-allowlist tool → drop silently. A noisy warning
                # every time Claude Code adds a new built-in would hurt
                # UX more than help.
                continue
            args = summarize_tool_call(name, block.get("input") or {})
            if args is None:
                continue
            calls.append(ToolCall(name=name, args=args))
    joined = "\n\n".join(t for t in texts if t.strip())
    return joined, calls


# ---------------------------------------------------------------------------
# Session assembly
# ---------------------------------------------------------------------------


@dataclass
class _SessionBuilder:
    """Mutable accumulator for a single session file."""

    id: str
    source: str = "claude_code"
    cwd: str | None = None
    model: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    turns: list[Turn] = field(default_factory=list)
    # Accumulated from tool-call args as turns are appended. Order-preserving,
    # de-duplicating — first write wins, so diff reviews show the path in
    # the order the session visited it.
    paths_touched: list[str] = field(default_factory=list)

    def observe_metadata(self, entry: _RawEntry) -> None:
        if self.cwd is None:
            cwd = entry.data.get("cwd")
            if isinstance(cwd, str):
                self.cwd = cwd
        if self.model is None:
            msg = entry.data.get("message")
            if isinstance(msg, dict):
                m = msg.get("model")
                if isinstance(m, str) and m:
                    self.model = m
        if entry.timestamp is not None:
            if self.started_at is None or entry.timestamp < self.started_at:
                self.started_at = entry.timestamp
            if self.ended_at is None or entry.timestamp > self.ended_at:
                self.ended_at = entry.timestamp

    def observe_paths_from_call(self, call: ToolCall) -> None:
        """Capture any `path` value the tool-call summariser kept."""
        p = call.args.get("path")
        if isinstance(p, str) and p and p not in self.paths_touched:
            self.paths_touched.append(p)

    def to_session(self, verbose: bool = False) -> Session | None:
        if not self.turns:
            return None
        # See the parallel comment in `cursor._SessionBuilder.to_session`:
        # the schema gates `text` on assistant turns by `session.verbose`,
        # so flip the flag whenever any turn would carry `text`. Avoids
        # a class of "we wrote a file we won't read" bugs at the seam.
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
            paths_touched=list(self.paths_touched),
            verbose=marker,
        )


def _build_session_from_file(
    path: Path, *, verbose: bool = False
) -> Session | None:
    session_id = path.stem
    builder = _SessionBuilder(id=session_id)

    for entry in _iter_jsonl(path):
        # Skip sub-agent / Task side chains in v0.1. They're referenced
        # via the parent's Task tool_use so the high-level story stays
        # coherent.
        if entry.is_sidechain:
            continue

        builder.observe_metadata(entry)

        msg = entry.data.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")

        if entry.type == "user" and role == "user":
            text = _extract_user_text(content)
            if text is None:
                continue  # tool_result-only row
            builder.turns.append(
                Turn(role="user", text=text, at=entry.timestamp)
            )
        elif entry.type == "assistant" and role == "assistant":
            summary_text, calls = _extract_assistant_pieces(content)
            summary = _first_sentence(summary_text)
            preview = (
                _preview(summary_text) if (verbose and summary_text) else None
            )
            raw_model = msg.get("model")
            turn_model: str | None = None
            if isinstance(raw_model, str) and raw_model.strip():
                turn_model = raw_model.strip()[:MAX_TURN_MODEL_CHARS]
            turn = Turn(
                role="assistant",
                summary=summary or None,
                text=preview,
                at=entry.timestamp,
                model=turn_model,
                tool_calls=calls,
            )
            # Skip purely empty assistant turns (no text, no calls). They
            # appear occasionally as streaming-chunk artifacts.
            if not summary and not calls and not turn.text:
                continue
            for call in calls:
                builder.observe_paths_from_call(call)
            builder.turns.append(turn)
        # Other entry types (file-history-snapshot, etc.) are dropped.

    session = builder.to_session(verbose=verbose)
    if session is None:
        return None
    # Safety net: re-validate against the schema before handing back. A
    # constraint added later should surface as an adapter bug, not silent
    # corruption of the .prompt file.
    validate_session(session)
    return session


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def claude_code_project_dir(bundle_root: Path) -> Path:
    """Directory Claude Code uses for this bundle's sessions (may not exist).

    This is the *exact-match* directory — sessions started directly in
    `bundle_root`. Sessions started in a subfolder of the bundle live in
    a sibling directory whose encoded name is a prefix-extension of this
    one; see ``_session_dir_candidates``.
    """
    return claude_code_store_root() / encode_bundle_path(bundle_root)


def _session_dir_candidates(bundle_root: Path) -> list[Path]:
    """Every Claude Code project dir that *could* belong to this bundle.

    Mirrors git's "any subdir of the worktree counts" rule. We start
    from the bundle's exact-encoded directory plus every sibling whose
    name extends it with a ``-`` separator — those are the candidates
    for sessions started in subdirectories of the bundle.

    The encoding is lossy (both ``/`` and ``-`` map to ``-``), so a
    sibling like ``-Users-alice-code-billing-old`` would also match
    when the bundle is ``billing``. That's intentional: this list is
    the *cheap* filter, and the caller cross-checks each session's
    actual ``cwd`` field against the bundle root before yielding it
    (see ``_session_belongs_to_bundle``).
    """
    store = claude_code_store_root()
    if not store.is_dir():
        return []
    encoded = encode_bundle_path(bundle_root)
    out: list[Path] = []
    for child in store.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if name == encoded or name.startswith(encoded + "-"):
            out.append(child)
    out.sort()
    return out


def _session_belongs_to_any(
    session: Session, resolved_roots: list[Path], *, strict_cwd: bool
) -> bool:
    """Is this session's recorded ``cwd`` inside *any* of the given roots?

    This is the defense-in-depth check that makes prompt scoping match
    git scoping. A session's ``cwd`` is recorded by Claude Code on
    every JSONL row; we read it during session assembly and compare
    it here against the union of roots the bundle has ever lived at
    (current path + historical aliases — see Fix #2).

    ``strict_cwd``:
      - ``False``  — the session file lived in one bundle-root's
        exact-encoded directory. Trust the directory name as a
        secondary signal: accept sessions even when ``cwd`` is missing
        (older Claude Code rows sometimes omit it). A *present* but
        out-of-tree ``cwd`` still rejects.
      - ``True``   — the session file lived in a prefix-extension
        directory (subfolder candidate). The encoding is lossy, so we
        must verify against ``cwd``. Missing ``cwd`` rejects.
    """
    raw = session.cwd
    if not raw:
        return not strict_cwd
    candidate = Path(raw)
    if not candidate.is_absolute():
        return False
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    for root in resolved_roots:
        if resolved == root or root in resolved.parents:
            return True
    return False


def read_claude_code_sessions(
    bundle_paths: Path | Iterable[Path],
    *,
    since: datetime | None = None,
    verbose: bool = False,
) -> Iterable[Session]:
    """Yield every Claude Code session captured for the given bundle.

    Accepts either a single bundle root (the common case) or an
    iterable of roots — current location plus every historical path
    the bundle has lived at. Pass the iterable form when you want
    rename-resilient discovery; the typical caller is ``prompts
    capture``, which routes through ``stage.historical_bundle_paths``
    so a moved bundle still finds its old sessions (Fix #2).

    Scope mirrors git: a session counts if Claude Code was launched in
    *any* recorded bundle root or any subdirectory of one. We discover
    candidate project directories by encoded-name prefix and then drop
    any session whose recorded ``cwd`` is outside every recorded root
    (defense-in-depth against Claude Code's lossy path encoding).

    ``since`` filters by session ``started_at``; callers use it for
    incremental sync. Sessions with the same id across multiple roots
    are de-duplicated within a single read (the same UUID file under
    two different bundle paths only yields once).
    """
    roots: list[Path] = (
        [bundle_paths]
        if isinstance(bundle_paths, Path)
        else list(bundle_paths)
    )
    if not roots:
        return  # type: ignore[return-value]

    # Collect candidate dirs across every root, remembering which exact
    # encoded name each root maps to so the cwd-strictness rule stays
    # per-root rather than per-set.
    exact_names: set[str] = {encode_bundle_path(r) for r in roots}
    seen_dirs: dict[Path, None] = {}
    for r in roots:
        for d in _session_dir_candidates(r):
            seen_dirs.setdefault(d, None)
    if not seen_dirs:
        return  # type: ignore[return-value]

    resolved_roots = [r.resolve() for r in roots]
    yielded_ids: set[str] = set()

    for project_dir in sorted(seen_dirs):
        # Strict cwd check unless this is the exact-encoded directory of
        # *some* recorded root. The encoded name alone can collide with
        # a sibling repo (e.g. `foo` vs `foo-bar`); the exact-match dirs
        # get the looser rule so we don't lose old sessions that happen
        # to lack a cwd row.
        strict = project_dir.name not in exact_names

        # Sort session files deterministically. Filename is `<uuid>.jsonl` —
        # UUID ordering is stable across runs, which gives us byte-stable
        # output from `prompts capture` given identical inputs.
        for path in sorted(project_dir.glob("*.jsonl")):
            try:
                session = _build_session_from_file(path, verbose=verbose)
            except Exception as e:  # noqa: BLE001
                # One bad file should not crash the whole sync. We
                # surface this as a soft adapter error; the command
                # layer formats it for the user.
                raise ClaudeCodeError(
                    f"{path.name}: could not build session — {e}"
                ) from e
            if session is None:
                continue
            if session.id in yielded_ids:
                continue
            if not _session_belongs_to_any(
                session, resolved_roots, strict_cwd=strict
            ):
                continue
            if since is not None and session.started_at is not None:
                if session.started_at < since:
                    continue
            yielded_ids.add(session.id)
            yield session
