"""
Codex adapter.

Reads Codex Desktop rollout JSONL files (under ``~/.codex``) and Cursor
in-editor Agent transcript JSONL files (under ``~/.cursor/projects/.../``),
then produces ``spec_cli.prompts.Session`` objects.

Cursor ``agent-transcripts`` sessions use ``source="cursor"``; only
OpenAI Codex Desktop rollouts use ``source="codex"``.

Store layout (Codex Desktop, observed):

  ~/.codex/state_5.sqlite
  ~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread-id>.jsonl

The SQLite DB indexes recent threads (title, cwd, rollout path, model). The
rollout JSONL is the source of truth for turns. This format is local and not
part of the Spec schema contract, so parsing is intentionally defensive.

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

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..bundle_scope import path_intersects_bundle
from ..prompts.schema import (
    MAX_TURN_MODEL_CHARS,
    MAX_TURN_TEXT_CHARS,
    Session,
    ToolCall,
    Turn,
    validate_session,
)
from ..prompts.text_sanitize import (
    is_cursor_redacted_placeholder,
    prose_without_redacted_placeholders,
    sanitize_for_toml_text,
    unwrap_cursor_user_message,
)
from ..prompts.tools import ALLOWED_TOOL_NAMES, summarize_tool_call

# This adapter currently reads Codex transcripts from Cursor's
# `agent-transcripts` storage layout.
_DEFAULT_CURSOR_HOME = "~/.cursor"
_DEFAULT_CODEX_HOME = "~/.codex"
_SUMMARY_CHARS: int = 200

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], bool], ...] = (
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[A-Za-z0-9._~+/=-]+"), True),
    (
        re.compile(
            r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret)\s*[=:]\s*)[^\s'\"`]+"
        ),
        True,
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), False),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"), False),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"), False),
)


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


def codex_cli_home() -> Path:
    """Codex Desktop / CLI home directory."""
    override = os.environ.get("CODEX_CLI_HOME")
    if override:
        return Path(override).expanduser()
    # ``CODEX_HOME`` already existed in this adapter as a test/store override.
    # If it points at a Desktop-shaped home, use it; otherwise keep the legacy
    # Cursor-projects meaning for ``codex_store_root`` intact.
    home = os.environ.get("CODEX_HOME")
    if home:
        return Path(home).expanduser()
    return Path(_DEFAULT_CODEX_HOME).expanduser()


def codex_desktop_index_path() -> Path:
    """SQLite thread index used by Codex Desktop."""
    return _codex_state_db()


def codex_transcript_store_available() -> bool:
    """Whether any supported Codex transcript store exists locally."""
    return codex_store_root().exists() or codex_desktop_index_path().exists()


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


def redact_text(text: str) -> str:
    """Redact common secrets before text is written into `.prompts`."""
    out = text
    for pat, keep_prefix in _SECRET_PATTERNS:
        out = pat.sub(
            lambda m: (m.group(1) if keep_prefix and m.lastindex else "")
            + "[REDACTED]",
            out,
        )
    return out


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
    if len(stripped) <= MAX_TURN_TEXT_CHARS:
        return stripped
    cut = stripped.rfind("\n\n", 0, MAX_TURN_TEXT_CHARS)
    if cut < MAX_TURN_TEXT_CHARS // 2:
        cut = stripped.rfind("\n", 0, MAX_TURN_TEXT_CHARS)
    if cut < MAX_TURN_TEXT_CHARS // 2:
        cut = MAX_TURN_TEXT_CHARS
    return stripped[:cut].rstrip() + "\n\n[…truncated…]"


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return sanitize_for_toml_text(redact_text(content))
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
            out.append(sanitize_for_toml_text(redact_text(text)))
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


@dataclass(frozen=True)
class CodexRecentSession:
    id: str
    title: str
    cwd: str
    path: Path
    updated_at: datetime | None = None
    model: str | None = None
    turn_count: int = 0


@dataclass
class _SessionBuilder:
    id: str
    source: str = "codex"
    turns: list[Turn] = field(default_factory=list)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    model: str | None = None
    title: str | None = None
    cwd: str | None = None
    paths_touched: list[str] = field(default_factory=list)

    def observe_timestamp(self, ts: datetime | None) -> None:
        if ts is None:
            return
        if self.started_at is None or ts < self.started_at:
            self.started_at = ts
        if self.ended_at is None or ts > self.ended_at:
            self.ended_at = ts

    def observe_paths_from_call(self, call: ToolCall) -> None:
        p = call.args.get("path")
        if isinstance(p, str) and p and p not in self.paths_touched:
            self.paths_touched.append(p)

    def to_session(self, *, verbose: bool, cwd: str | None) -> Session | None:
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
            cwd=cwd or self.cwd,
            title=self.title,
            paths_touched=self.paths_touched,
            verbose=marker,
        )


def _content_text_from_response_item(content: Any) -> str:
    if isinstance(content, str):
        return sanitize_for_toml_text(redact_text(content))
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype not in {"output_text", "input_text", "text"}:
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(sanitize_for_toml_text(redact_text(text)))
    return "\n\n".join(p for p in parts if p.strip())


def _session_id_from_rollout_path(path: Path) -> str:
    stem = path.stem
    # rollout-2026-05-08T16-10-21-<uuid-ish>
    parts = stem.split("-")
    if len(parts) >= 8:
        return "-".join(parts[-5:])
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    return f"codex-{digest}"


_CODEX_TOOL_NAME_MAP: dict[str, str] = {
    # Codex Desktop / OpenAI Responses API function names → canonical
    # spec tool names. Same idea as ``_CURSOR_TOOL_NAME_MAP`` —
    # normalize on the wire so reviewers see ``Edit auth.py`` whether
    # the agent was Cursor, Codex, or Claude Code.
    "read_file": "Read",
    "shell": "Bash",
    "container.exec": "Bash",
    "local_shell": "Bash",
    "apply_patch": "Edit",
    "write_file": "Write",
    "create_file": "Write",
    "edit_file": "Edit",
    "grep": "Grep",
    "search": "Grep",
    "glob": "Glob",
    "list_dir": "Glob",
    "web_search": "WebSearch",
    "fetch": "WebFetch",
}


def _codex_function_call_to_tool(payload: dict) -> ToolCall | None:
    """Decode a Codex rollout ``response_item.type == "function_call"``
    (OpenAI Responses API shape) into a canonical :class:`ToolCall`.

    Codex emits each tool invocation as its own JSONL row, separate
    from the surrounding ``message`` rows. We coalesce these onto the
    next assistant turn via :func:`_build_codex_rollout_session` so a
    reviewer sees a single "the AI replied" line carrying the prose
    body and the ordered tool list.
    """
    raw_name = payload.get("name")
    if not isinstance(raw_name, str) or not raw_name:
        return None
    canonical = _CODEX_TOOL_NAME_MAP.get(raw_name)
    if canonical is None or canonical not in ALLOWED_TOOL_NAMES:
        return None
    args_raw = payload.get("arguments") or payload.get("input") or {}
    args: dict = {}
    if isinstance(args_raw, str) and args_raw.strip():
        try:
            parsed = json.loads(args_raw)
            if isinstance(parsed, dict):
                args = parsed
        except json.JSONDecodeError:
            pass
    elif isinstance(args_raw, dict):
        args = args_raw
    summarized = summarize_tool_call(canonical, args)
    if summarized is None:
        return None
    return ToolCall(name=canonical, args=summarized)


def _build_codex_rollout_session(
    path: Path,
    *,
    cwd: str | None = None,
    title: str | None = None,
    model: str | None = None,
    verbose: bool = False,
) -> Session | None:
    builder = _SessionBuilder(id=_session_id_from_rollout_path(path))
    builder.cwd = cwd
    builder.title = title
    builder.model = model

    # Tool calls Codex emitted in JSONL rows *between* the previous
    # assistant message and the next. Attached to the upcoming
    # assistant turn so a reviewer sees one event per logical reply
    # carrying the prose body + the ordered tool list — mirrors what
    # the Cursor adapter does with type-2 bubbles.
    pending_tool_calls: list[ToolCall] = []

    for row in _iter_jsonl(path):
        ts = _parse_timestamp(row.get("timestamp"))
        builder.observe_timestamp(ts)
        rtype = row.get("type")
        payload = row.get("payload")
        if rtype == "session_meta" and isinstance(payload, dict):
            sid = payload.get("id")
            if isinstance(sid, str) and sid.strip():
                builder.id = sid.strip()
            pcwd = payload.get("cwd")
            if builder.cwd is None and isinstance(pcwd, str) and pcwd.strip():
                builder.cwd = pcwd.strip()
            pmodel = payload.get("model") or payload.get("model_slug")
            if builder.model is None and isinstance(pmodel, str) and pmodel.strip():
                builder.model = pmodel.strip()[:MAX_TURN_MODEL_CHARS]
            continue

        if rtype == "event_msg" and isinstance(payload, dict):
            ptype = payload.get("type")
            if ptype == "user_message":
                text = payload.get("message")
                if isinstance(text, str) and text.strip():
                    builder.turns.append(
                        Turn(
                            role="user",
                            text=sanitize_for_toml_text(redact_text(text)),
                            at=ts,
                        )
                    )
            continue

        if rtype != "response_item" or not isinstance(payload, dict):
            continue
        ptype = payload.get("type")
        # Function call rows are siblings of message rows in the
        # Responses API rollout. Accumulate them onto the next
        # assistant message so the wire carries "prose + structured
        # tool list" in one event.
        if ptype in ("function_call", "local_shell_call"):
            call = _codex_function_call_to_tool(payload)
            if call is not None:
                pending_tool_calls.append(call)
                p = call.args.get("path")
                if isinstance(p, str) and p and p not in builder.paths_touched:
                    builder.paths_touched.append(p)
            continue
        if ptype != "message":
            continue
        role = payload.get("role")
        if role == "user":
            text = _content_text_from_response_item(payload.get("content"))
            if text.strip():
                # A new user prompt closes the pending tool list for
                # the previous assistant turn. If we got here without
                # ever seeing a closing assistant message, attach the
                # tools to a synthetic assistant turn — better than
                # silently dropping the agent's actions.
                if pending_tool_calls:
                    builder.turns.append(
                        Turn(
                            role="assistant",
                            summary=None,
                            text=None,
                            at=ts,
                            model=builder.model,
                            tool_calls=list(pending_tool_calls),
                        )
                    )
                    pending_tool_calls = []
                builder.turns.append(Turn(role="user", text=text, at=ts))
            continue
        if role != "assistant":
            continue
        text = _content_text_from_response_item(payload.get("content"))
        # A truly empty assistant message with no pending tools is
        # almost always a streaming artefact — drop it. Otherwise we
        # always emit a turn so the structured tool list lands.
        if not text.strip() and not pending_tool_calls:
            continue
        summary = _first_sentence(text) if text.strip() else None
        preview_text = _preview(text) if (verbose and text.strip()) else None
        builder.turns.append(
            Turn(
                role="assistant",
                summary=summary or None,
                text=preview_text,
                at=ts,
                model=builder.model,
                tool_calls=list(pending_tool_calls),
            )
        )
        pending_tool_calls = []

    # Trailing function calls with no closing assistant message — emit
    # them so the reviewer still sees what the agent did.
    if pending_tool_calls:
        builder.turns.append(
            Turn(
                role="assistant",
                summary=None,
                text=None,
                at=None,
                model=builder.model,
                tool_calls=list(pending_tool_calls),
            )
        )

    session = builder.to_session(verbose=verbose, cwd=builder.cwd)
    if session is None:
        return None
    if not session.title:
        first_user = next((t.text for t in session.turns if t.role == "user" and t.text), None)
        session.title = _first_sentence(first_user or "")[:120] or path.stem
    validate_session(session)
    return session


def _path_intersects_bundle(cwd: str | None, bundle_paths: Iterable[Path]) -> bool:
    if not cwd:
        return False
    candidate = Path(cwd)
    for root in bundle_paths:
        if path_intersects_bundle(candidate, root):
            return True
    return False


def _codex_state_db(home: Path | None = None) -> Path:
    return (home or codex_cli_home()) / "state_5.sqlite"


def list_recent_codex_sessions(
    bundle_paths: Path | Iterable[Path],
    *,
    limit: int = 20,
) -> list[CodexRecentSession]:
    """Return recent Codex Desktop chats whose cwd intersects this bundle."""
    roots: list[Path] = [bundle_paths] if isinstance(bundle_paths, Path) else list(bundle_paths)
    db = _codex_state_db()
    if not db.is_file():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute(
            """
            SELECT id, title, cwd, rollout_path, updated_at_ms, model
            FROM threads
            WHERE archived = 0
            ORDER BY updated_at_ms DESC, id DESC
            LIMIT 200
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass

    out: list[CodexRecentSession] = []
    for sid, title, cwd, rollout_path, updated_ms, model in rows:
        if not isinstance(rollout_path, str) or not rollout_path:
            continue
        path = Path(rollout_path).expanduser()
        if not path.is_file():
            continue
        if not _path_intersects_bundle(cwd if isinstance(cwd, str) else None, roots):
            continue
        updated = _parse_timestamp(updated_ms)
        turn_count = _count_codex_rollout_turns(path)
        out.append(
            CodexRecentSession(
                id=str(sid),
                title=(title if isinstance(title, str) and title.strip() else "(untitled)"),
                cwd=str(cwd) if isinstance(cwd, str) else "",
                path=path,
                updated_at=updated,
                model=model if isinstance(model, str) and model.strip() else None,
                turn_count=turn_count,
            )
        )
        if len(out) >= limit:
            break
    return out


def _count_codex_rollout_turns(path: Path) -> int:
    n = 0
    for row in _iter_jsonl(path):
        rtype = row.get("type")
        payload = row.get("payload")
        if (
            rtype == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "user_message"
        ):
            if isinstance(payload.get("message"), str) and payload.get("message", "").strip():
                n += 1
        elif rtype == "response_item" and isinstance(payload, dict):
            if payload.get("type") == "message" and payload.get("role") in {"user", "assistant"}:
                if _content_text_from_response_item(payload.get("content")).strip():
                    n += 1
    return n


def read_codex_rollout_session(
    path: Path,
    *,
    verbose: bool = True,
    title: str | None = None,
    cwd: str | None = None,
    model: str | None = None,
) -> Session | None:
    """Read one Codex Desktop rollout JSONL file."""
    return _build_codex_rollout_session(
        path, cwd=cwd, title=title, model=model, verbose=verbose
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
    # JSONL under ``~/.cursor/projects/.../agent-transcripts/`` is Cursor
    # in-editor Agent storage — not OpenAI Codex Desktop. Label it
    # ``cursor`` so Spec Live / capture match what users actually ran.
    #
    # Cursor emits one JSONL row per agent step. Many steps carry only the
    # literal ``[REDACTED]`` prose placeholder (tool args are still in the
    # row). Treating each row as its own :class:`Turn` made ``spec watch``
    # POST dozens of useless assistant events and ``spec team watch`` print
    # a wall of empty AI lines. Coalesce consecutive assistant rows into one
    # logical turn — same shape as the Composer adapter in ``cursor.py``.
    builder = _SessionBuilder(id=path.stem, source="cursor")
    pending_texts: list[str] = []
    pending_tool_calls: list[ToolCall] = []
    pending_first_at: datetime | None = None
    pending_last_at: datetime | None = None
    pending_model: str | None = None
    pending_has_activity = False

    def _flush_assistant() -> None:
        nonlocal pending_texts, pending_tool_calls, pending_first_at
        nonlocal pending_last_at, pending_model, pending_has_activity
        if not pending_has_activity:
            return
        joined = "\n\n".join(
            t.strip() for t in pending_texts if t.strip()
        ).strip()
        summary: str | None = _first_sentence(joined) if joined else None
        if summary and is_cursor_redacted_placeholder(summary):
            summary = None
        preview_text = _preview(joined) if (verbose and joined) else None
        if not summary and not preview_text and not pending_tool_calls:
            pending_texts = []
            pending_tool_calls = []
            pending_first_at = None
            pending_last_at = None
            pending_model = None
            pending_has_activity = False
            return
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
        pending_texts = []
        pending_tool_calls = []
        pending_first_at = None
        pending_last_at = None
        pending_model = None
        pending_has_activity = False

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
            _flush_assistant()
            body = unwrap_cursor_user_message(text)
            if not body.strip():
                continue
            builder.turns.append(Turn(role="user", text=body, at=ts))
            continue

        model_raw = msg.get("model") or row.get("model")
        turn_model: str | None = None
        if isinstance(model_raw, str) and model_raw.strip():
            turn_model = model_raw.strip()[:MAX_TURN_MODEL_CHARS]
        if pending_model is None and turn_model is not None:
            pending_model = turn_model
        if pending_first_at is None:
            pending_first_at = ts
        if ts is not None:
            pending_last_at = ts

        calls = _extract_tool_calls(content)
        if calls:
            pending_tool_calls.extend(calls)
            for call in calls:
                builder.observe_paths_from_call(call)
            pending_has_activity = True

        prose = prose_without_redacted_placeholders(text)
        if prose:
            pending_texts.append(prose)
            pending_has_activity = True
        elif calls:
            pending_has_activity = True

    _flush_assistant()

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


def iter_cursor_agent_transcript_sessions(
    bundle_paths: Path | Iterable[Path],
    *,
    since: datetime | None = None,
    verbose: bool = False,
) -> Iterable[Session]:
    """Yield Cursor in-editor Agent sessions from JSONL under ``agent-transcripts``.

    Cursor stores these under ``<CURSOR_HOME or ~/.cursor>/projects/<encoded>/``.
    Same on-disk layout historically reached through :func:`read_codex_sessions`;
    that path is now owned here so ``source`` is consistently ``cursor`` and
    :func:`read_cursor_sessions` can merge them with Composer data.
    """
    roots: list[Path] = [bundle_paths] if isinstance(bundle_paths, Path) else list(bundle_paths)
    if not roots:
        return  # type: ignore[return-value]
    yielded: set[str] = set()
    candidates = _project_dir_candidates(roots)
    if not candidates:
        return  # type: ignore[return-value]

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


def read_codex_sessions(
    bundle_paths: Path | Iterable[Path],
    *,
    since: datetime | None = None,
    verbose: bool = False,
) -> Iterable[Session]:
    """Yield OpenAI Codex Desktop rollout sessions for the current bundle.

    Cursor in-editor Agent JSONL lives under ``~/.cursor/projects/`` — use
    :func:`read_cursor_sessions` (which calls :func:`iter_cursor_agent_transcript_sessions`)
    so those threads are labeled ``source=cursor`` and merged with Composer.
    """
    roots: list[Path] = [bundle_paths] if isinstance(bundle_paths, Path) else list(bundle_paths)
    if not roots:
        return  # type: ignore[return-value]
    yielded: set[str] = set()

    for recent in list_recent_codex_sessions(roots, limit=1000):
        if recent.id in yielded:
            continue
        try:
            session = read_codex_rollout_session(
                recent.path,
                verbose=verbose,
                title=recent.title,
                cwd=recent.cwd,
                model=recent.model,
            )
        except Exception as e:  # noqa: BLE001
            raise CodexError(
                f"{recent.path.name}: could not build session — {e}"
            ) from e
        if session is None:
            continue
        if since is not None and session.started_at is not None and session.started_at < since:
            continue
        yielded.add(session.id)
        yield session
