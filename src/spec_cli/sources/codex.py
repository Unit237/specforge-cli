"""
Codex adapter.

Reads Cursor agent transcript JSONL files and produces
``spec_cli.prompts.Session`` objects.

Store layout (Cursor):

  ~/.cursor/projects/<encoded-workspace>/agent-transcripts/<session-id>/<session-id>.jsonl

`<encoded-workspace>` mirrors the absolute workspace path with `/`
replaced by `-` and no leading slash, for example:

  /Users/alice/code/spec -> Users-alice-code-spec

This adapter mirrors git-style scoping used by other sources:
sessions count when they belong to the bundle root or one of its
subdirectories (implemented as encoded-prefix matching on project dir
names). Unlike Claude Code, transcript rows do not reliably carry a
cwd field, so directory scoping is the primary signal.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..prompts.schema import (
    MAX_TURN_MODEL_CHARS,
    Session,
    ToolCall,
    Turn,
    validate_session,
)
from ..prompts.text_sanitize import sanitize_for_toml_text
from ..prompts.tools import ALLOWED_TOOL_NAMES, summarize_tool_call

# This adapter currently reads Codex transcripts from Cursor's
# `agent-transcripts` storage layout.
_DEFAULT_CURSOR_HOME = "~/.cursor"
_SUMMARY_CHARS: int = 200
_PREVIEW_CHARS: int = 4000


class CodexError(RuntimeError):
    """Raised for adapter-level failures while reading transcripts."""


def codex_store_root() -> Path:
    """Root directory containing per-workspace Codex transcript stores."""
    # Priority:
    #  1) CODEX_HOME   - explicit override for this adapter
    #  2) CURSOR_HOME  - shared Cursor install override
    #  3) ~/.cursor    - default Cursor user-data home
    override = os.environ.get("CODEX_HOME") or os.environ.get("CURSOR_HOME")
    base = Path(override).expanduser() if override else Path(_DEFAULT_CURSOR_HOME).expanduser()
    return base / "projects"


def encode_bundle_path(bundle_root: Path) -> str:
    """Encode bundle root to Cursor's project-directory naming convention."""
    resolved = bundle_root.resolve().as_posix().lstrip("/")
    return resolved.replace("/", "-")


def codex_project_dir(bundle_root: Path) -> Path:
    """Project directory where Codex transcripts for this bundle live."""
    return codex_store_root() / encode_bundle_path(bundle_root)


def _parse_timestamp(raw: Any) -> datetime | None:
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
    if isinstance(raw, (int, float)) and raw > 0:
        seconds = raw / 1000.0 if raw > 1e12 else float(raw)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    return None


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
    stripped = text.strip()
    if len(stripped) <= _PREVIEW_CHARS:
        return stripped
    cut = stripped.rfind("\n\n", 0, _PREVIEW_CHARS)
    if cut < _PREVIEW_CHARS // 2:
        cut = stripped.rfind("\n", 0, _PREVIEW_CHARS)
    if cut < _PREVIEW_CHARS // 2:
        cut = _PREVIEW_CHARS
    return stripped[:cut].rstrip() + "\n\n[…truncated…]"


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return sanitize_for_toml_text(content)
    if not isinstance(content, list):
        return ""
    out: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            out.append(sanitize_for_toml_text(text))
    return "\n\n".join(t for t in out if t.strip())


def _extract_tool_calls(content: Any) -> list[ToolCall]:
    if not isinstance(content, list):
        return []
    out: list[ToolCall] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype not in {"tool_use", "tool_call"}:
            continue
        name = block.get("name")
        if not isinstance(name, str) or not name or name not in ALLOWED_TOOL_NAMES:
            continue
        args = summarize_tool_call(name, block.get("input") or block.get("args") or {})
        if args is None:
            continue
        out.append(ToolCall(name=name, args=args))
    return out


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


@dataclass
class _SessionBuilder:
    id: str
    source: str = "codex"
    turns: list[Turn] = field(default_factory=list)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    model: str | None = None

    def observe_timestamp(self, ts: datetime | None) -> None:
        if ts is None:
            return
        if self.started_at is None or ts < self.started_at:
            self.started_at = ts
        if self.ended_at is None or ts > self.ended_at:
            self.ended_at = ts

    def to_session(self, *, verbose: bool, cwd: str) -> Session | None:
        if not self.turns:
            return None
        marker = verbose or any(t.role == "assistant" and t.text for t in self.turns)
        return Session(
            id=self.id,
            source=self.source,
            turns=self.turns,
            started_at=self.started_at,
            ended_at=self.ended_at,
            model=self.model,
            cwd=cwd,
            verbose=marker,
        )


def _project_dir_candidates(bundle_paths: Iterable[Path]) -> list[tuple[Path, Path]]:
    root = codex_store_root()
    if not root.is_dir():
        return []
    resolved_roots = [p.resolve() for p in bundle_paths]
    if not resolved_roots:
        return []
    prefixes = [encode_bundle_path(p) for p in resolved_roots]
    out: list[tuple[Path, Path]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        for i, prefix in enumerate(prefixes):
            if name == prefix or name.startswith(prefix + "-"):
                out.append((child, resolved_roots[i]))
                break
    return out


def _build_session(path: Path, *, cwd: Path, verbose: bool) -> Session | None:
    builder = _SessionBuilder(id=path.stem)
    for row in _iter_jsonl(path):
        role = row.get("role")
        if role not in {"user", "assistant"}:
            continue
        msg = row.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        text = _extract_text(content)
        ts = _parse_timestamp(
            row.get("timestamp")
            or row.get("at")
            or msg.get("timestamp")
            or msg.get("created_at")
        )
        builder.observe_timestamp(ts)
        if role == "user":
            if not text.strip():
                continue
            builder.turns.append(Turn(role="user", text=text, at=ts))
            continue

        model_raw = msg.get("model") or row.get("model")
        turn_model: str | None = None
        if isinstance(model_raw, str) and model_raw.strip():
            turn_model = model_raw.strip()[:MAX_TURN_MODEL_CHARS]
        calls = _extract_tool_calls(content)
        summary = _first_sentence(text)
        preview_text = _preview(text) if (verbose and text) else None
        if not summary and not preview_text and not calls:
            continue
        builder.turns.append(
            Turn(
                role="assistant",
                summary=summary or None,
                text=preview_text,
                at=ts,
                model=turn_model,
                tool_calls=calls,
            )
        )
        if builder.model is None and turn_model:
            builder.model = turn_model

    if builder.started_at is None:
        try:
            builder.started_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            builder.ended_at = builder.started_at
        except OSError:
            pass
    session = builder.to_session(verbose=verbose, cwd=str(cwd))
    if session is None:
        return None
    validate_session(session)
    return session


def read_codex_sessions(
    bundle_paths: Path | Iterable[Path],
    *,
    since: datetime | None = None,
    verbose: bool = False,
) -> Iterable[Session]:
    """Yield Codex sessions for the current bundle scope."""
    roots: list[Path] = [bundle_paths] if isinstance(bundle_paths, Path) else list(bundle_paths)
    if not roots:
        return  # type: ignore[return-value]
    candidates = _project_dir_candidates(roots)
    if not candidates:
        return  # type: ignore[return-value]

    yielded: set[str] = set()
    for project_dir, anchor in candidates:
        transcripts_dir = project_dir / "agent-transcripts"
        if not transcripts_dir.is_dir():
            continue
        for session_dir in sorted(transcripts_dir.iterdir()):
            if not session_dir.is_dir():
                continue
            path = session_dir / f"{session_dir.name}.jsonl"
            if not path.is_file():
                continue
            if path.stem in yielded:
                continue
            try:
                session = _build_session(path, cwd=anchor, verbose=verbose)
            except Exception as e:  # noqa: BLE001
                raise CodexError(f"{path.name}: could not build session — {e}") from e
            if session is None:
                continue
            if since is not None and session.started_at is not None and session.started_at < since:
                continue
            yielded.add(session.id)
            yield session
