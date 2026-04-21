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

from ..prompts.schema import Session, ToolCall, Turn, validate_session
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


def _extract_user_text(content: Any) -> str | None:
    """Pull the text from a Claude Code `user` message.

    Returns None if the message is purely a tool_result (not a real user
    turn in our model).
    """
    if isinstance(content, str):
        return content if content.strip() else None
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
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
                texts.append(text)
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
        return Session(
            id=self.id,
            source=self.source,
            turns=self.turns,
            started_at=self.started_at,
            ended_at=self.ended_at,
            cwd=self.cwd,
            model=self.model,
            paths_touched=list(self.paths_touched),
            verbose=verbose,
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
            turn = Turn(
                role="assistant",
                summary=summary or None,
                text=summary_text if verbose else None,
                at=entry.timestamp,
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
    """Directory Claude Code uses for this bundle's sessions (may not exist)."""
    return claude_code_store_root() / encode_bundle_path(bundle_root)


def read_claude_code_sessions(
    bundle_root: Path,
    *,
    since: datetime | None = None,
    verbose: bool = False,
) -> Iterable[Session]:
    """Yield every Claude Code session captured for `bundle_root`.

    `since` filters by session `started_at` — callers use this for
    incremental sync.
    """
    project_dir = claude_code_project_dir(bundle_root)
    if not project_dir.is_dir():
        return  # type: ignore[return-value]

    # Sort session files deterministically. Filename is `<uuid>.jsonl` —
    # UUID ordering is stable across runs, which gives us byte-stable output
    # from `prompts sync` given identical inputs.
    for path in sorted(project_dir.glob("*.jsonl")):
        try:
            session = _build_session_from_file(path, verbose=verbose)
        except Exception as e:  # noqa: BLE001
            # One bad file should not crash the whole sync. We surface
            # this as a soft adapter error; the command layer formats it
            # for the user.
            raise ClaudeCodeError(
                f"{path.name}: could not build session — {e}"
            ) from e
        if session is None:
            continue
        if since is not None and session.started_at is not None:
            if session.started_at < since:
                continue
        yield session
