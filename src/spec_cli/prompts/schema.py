"""
`.prompts` file schema — parse, validate, and represent a commit's worth of
conversational sessions. See `docs/prompt-format.md` for the authoritative
contract.

A `.prompts` file contains:

  - one `[commit]` table (branch + author identity + message)
  - one or more `[[sessions]]` blocks (each with its own title, summary,
    outcome, visibility, tool-call turns, and so on)
  - an append-only `[[edits]]` log

We deliberately do NOT build an exhaustive YAML-style schema library. The
format is small, the rules are precise, and explicit conditionals produce
better error messages. Unknown keys are a hard error so typos surface loudly
(docs/prompt-format.md §validation rules).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib as _tomllib
else:  # pragma: no cover - exercised on 3.9/3.10
    import tomli as _tomllib

from .tools import ALLOWED_TOOL_NAMES, summarize_tool_call


# Current schema revision. Written into every rendered file; parser accepts
# absent (legacy-shaped hand-authored files) and equal; a newer revision is
# rejected with a clear upgrade hint.
SCHEMA_VERSION: str = "spec.prompts/v0.1"

VALID_SOURCES: frozenset[str] = frozenset({"claude_code", "cursor", "manual"})
VALID_ROLES: frozenset[str] = frozenset({"user", "assistant"})
# `tool_result` is reserved — see docs/prompt-format.md. Rejected in v0.1.
RESERVED_ROLES: frozenset[str] = frozenset({"tool_result"})
VALID_OUTCOMES: frozenset[str] = frozenset(
    {"shipped", "abandoned", "exploratory", "failed"}
)
VALID_VISIBILITIES: frozenset[str] = frozenset({"public", "private"})
DEFAULT_VISIBILITY: str = "public"

# Per-turn `text` cap: user (required) or assistant when `session.verbose` (rule 8).
# 32 KiB was too small for real captures (pasted logs, big patches). Backed by Text in DB.
MAX_TURN_TEXT_CHARS: int = 512 * 1024
MAX_USER_TEXT_CHARS: int = MAX_TURN_TEXT_CHARS  # same cap; kept for API compatibility
MAX_SUMMARY_CHARS: int = 2000
MAX_TITLE_CHARS: int = 200
MAX_LESSON_CHARS: int = 500

_COMMIT_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "branch",
        "message",
        "committed_at",
        "author_name",
        "author_email",
        "author_username",
    }
)
_SESSION_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "source",
        "started_at",
        "ended_at",
        "model",
        "cwd",
        "operator",
        "title",
        "summary",
        "lesson",
        "tags",
        "outcome",
        "visibility",
        "forked_from",
        "paths_touched",
        "verbose",
        # Per-session commit context. When the file holds sessions from
        # many commits on the same branch (the post-v0.2 default), this
        # block carries each session's own commit attribution. The
        # file-level [commit] still records the *branch* identity, but
        # individual sessions point at their own SHAs through here.
        "commit",
        # Review provenance. Set when the session's branch was merged
        # into trunk through a Cloud branch review — the green-dot
        # signal in the trunk prompts file UI. None on direct-to-trunk
        # captures (no review involved) and on unmerged branches.
        "merged_from",  # str — name of the branch the session merged from
        "merged_at",    # datetime — when the merge happened
        "approved_by",  # str — handle / email of who approved the review
    }
)
_SESSION_COMMIT_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "branch",
        "commit_sha",
        "message",
        "committed_at",
        "author_name",
        "author_email",
        "author_username",
    }
)
_EDIT_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"at", "by", "sessions", "turns", "reason"}
)
_TURN_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"role", "at", "text", "summary", "tool_calls"}
)
_TOOL_CALL_ALLOWED_KEYS: frozenset[str] = frozenset({"name", "args", "status"})


class PromptSchemaError(ValueError):
    """Raised when a `.prompts` file (or in-memory PromptsFile) violates the
    schema. The message always starts with a relative locator
    (e.g. `commit.branch`, `sessions[0].turns[2].tool_calls[0].name`) to
    make errors actionable."""

    def __init__(self, message: str, *, path: str = ""):
        location = f"{path}: " if path else ""
        super().__init__(f"{location}{message}")
        self.path = path


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    status: str | None = None


@dataclass
class Turn:
    role: str
    text: str | None = None
    summary: str | None = None
    at: datetime | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class SessionCommit:
    """Per-session commit context.

    A `.prompts` file under the v0.2+ "one file per branch" shape can
    hold sessions from many commits — each commit on the branch gets
    its own `[sessions.commit]` block so attribution is preserved.
    The file-level `[commit]` continues to record the *branch* identity
    (which is the one piece of data that is constant across every
    session in the file).
    """

    branch: str | None = None
    commit_sha: str | None = None
    message: str | None = None
    committed_at: datetime | None = None
    author_name: str | None = None
    author_email: str | None = None
    author_username: str | None = None


@dataclass
class Session:
    """One conversational session. Many of these ride inside a single
    `.prompts` file."""

    id: str
    source: str
    turns: list[Turn] = field(default_factory=list)

    # Capture-time context
    started_at: datetime | None = None
    ended_at: datetime | None = None
    model: str | None = None
    cwd: str | None = None
    operator: str | None = None

    # Author-written, drives discovery in the `/prompts` feed
    title: str | None = None
    summary: str | None = None
    lesson: str | None = None
    tags: list[str] = field(default_factory=list)
    outcome: str | None = None
    visibility: str = DEFAULT_VISIBILITY

    # Lineage / capture signal
    forked_from: str | None = None
    paths_touched: list[str] = field(default_factory=list)

    # When true, assistant turns may carry `text`
    verbose: bool = False

    # Per-session commit context (v0.2+). None on legacy files where
    # the file-level [commit] block carried this for the only commit.
    commit: SessionCommit | None = None

    # Review provenance — the "green dot" signal. Set ONLY when the
    # session arrived on trunk's prompts file via a Cloud branch
    # review merge. Direct-to-trunk captures and unmerged branch
    # captures all leave these None.
    merged_from: str | None = None
    merged_at: datetime | None = None
    approved_by: str | None = None


@dataclass
class CommitMeta:
    """Per-commit identity captured at write time.

    A `.prompts` file is tied to the git commit that introduced it; this
    block records enough to attribute the commit without touching git
    again on read.
    """

    branch: str
    author_name: str
    author_email: str
    message: str | None = None
    committed_at: datetime | None = None
    author_username: str | None = None


@dataclass
class PromptsEdit:
    """One entry in the file-level append-only edit log."""

    at: datetime
    by: str
    sessions: list[str] = field(default_factory=list)
    turns: list[int] = field(default_factory=list)
    reason: str | None = None


@dataclass
class PromptsFile:
    """In-memory representation of a whole `.prompts` file."""

    commit: CommitMeta
    sessions: list[Session] = field(default_factory=list)
    edits: list[PromptsEdit] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _require_str(value: Any, *, path: str, default: str | None = None) -> str:
    if value is None:
        if default is not None:
            return default
        raise PromptSchemaError("expected a string", path=path)
    if not isinstance(value, str):
        raise PromptSchemaError("expected a string", path=path)
    return value


def _optional_str(value: Any, *, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PromptSchemaError("expected a string", path=path)
    return value


def _optional_str_list(value: Any, *, path: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PromptSchemaError("expected a list of strings", path=path)
    out: list[str] = []
    for i, v in enumerate(value):
        if not isinstance(v, str):
            raise PromptSchemaError("expected a string", path=f"{path}[{i}]")
        out.append(v)
    return out


def _as_utc_datetime(value: Any, *, path: str) -> datetime:
    """TOML parsers return `datetime` objects for datetimes. We force UTC so
    rendered files are byte-stable regardless of the source's TZ."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # TOML local datetime — assume UTC (our renderer always emits Z).
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
                timezone.utc
            )
        except ValueError as e:
            raise PromptSchemaError(
                f"invalid timestamp `{value}` — expected RFC3339 (e.g. 2026-03-10T11:47:12Z)",
                path=path,
            ) from e
    raise PromptSchemaError("expected a datetime", path=path)


def _reject_unknown_keys(
    data: dict[str, Any], allowed: frozenset[str], *, path: str
) -> None:
    unknown = set(data.keys()) - allowed
    if unknown:
        raise PromptSchemaError(
            f"unknown key(s): {sorted(unknown)}. Allowed: {sorted(allowed)}",
            path=path,
        )


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def parse_prompts_text(text: str) -> PromptsFile:
    """Parse + validate a full `.prompts` file from its textual form."""
    try:
        data = _tomllib.loads(text)
    except Exception as e:  # tomli and tomllib raise different types
        raise PromptSchemaError(f"not valid TOML: {e}") from e

    if not isinstance(data, dict):
        raise PromptSchemaError("top-level must be a TOML table")

    # Optional schema header. If present, gate on version so a v0.2 file
    # gets a clear error on a v0.1 CLI.
    schema = data.get("schema")
    if schema is not None:
        schema = _require_str(schema, path="schema")
        if schema != SCHEMA_VERSION:
            raise PromptSchemaError(
                f"unsupported schema `{schema}`. This CLI understands "
                f"`{SCHEMA_VERSION}`. Upgrade `spec-cli` or pin the file.",
                path="schema",
            )

    commit = _parse_commit(data.get("commit"), path="commit")
    sessions = _parse_sessions(data.get("sessions"))
    edits = _parse_edits(data.get("edits"))

    pf = PromptsFile(commit=commit, sessions=sessions, edits=edits)
    validate_prompts_file(pf)
    return pf


def read_prompts_file(path: Path) -> PromptsFile:
    """Read + validate a `.prompts` file from disk."""
    text = path.read_text(encoding="utf-8")
    try:
        return parse_prompts_text(text)
    except PromptSchemaError as e:
        # Prepend the file path so `prompts validate` output is directly
        # jump-to-line-friendly.
        raise PromptSchemaError(
            f"{e}", path=f"{path}::{e.path}" if e.path else str(path)
        ) from e


def _parse_commit(raw: Any, *, path: str) -> CommitMeta:
    if raw is None:
        raise PromptSchemaError("missing required [commit] table", path=path)
    if not isinstance(raw, dict):
        raise PromptSchemaError("expected a table", path=path)
    _reject_unknown_keys(raw, _COMMIT_ALLOWED_KEYS, path=path)

    branch = _require_str(raw.get("branch"), path=f"{path}.branch")
    if not branch.strip():
        raise PromptSchemaError("must be non-empty", path=f"{path}.branch")

    author_name = _require_str(raw.get("author_name"), path=f"{path}.author_name")
    if not author_name.strip():
        raise PromptSchemaError("must be non-empty", path=f"{path}.author_name")

    author_email = _require_str(raw.get("author_email"), path=f"{path}.author_email")
    if not author_email.strip():
        raise PromptSchemaError("must be non-empty", path=f"{path}.author_email")

    committed_at_raw = raw.get("committed_at")
    committed_at = (
        _as_utc_datetime(committed_at_raw, path=f"{path}.committed_at")
        if committed_at_raw is not None
        else None
    )

    return CommitMeta(
        branch=branch,
        author_name=author_name,
        author_email=author_email,
        message=_optional_str(raw.get("message"), path=f"{path}.message"),
        committed_at=committed_at,
        author_username=_optional_str(
            raw.get("author_username"), path=f"{path}.author_username"
        ),
    )


def _parse_sessions(raw: Any) -> list[Session]:
    if raw is None:
        raise PromptSchemaError(
            "at least one [[sessions]] block is required", path="sessions"
        )
    if not isinstance(raw, list):
        raise PromptSchemaError("expected an array of tables", path="sessions")
    if len(raw) == 0:
        raise PromptSchemaError(
            "at least one [[sessions]] block is required", path="sessions"
        )
    out: list[Session] = []
    for i, item in enumerate(raw):
        out.append(_parse_session(item, index=i))
    return out


def _parse_session(raw: Any, *, index: int) -> Session:
    path = f"sessions[{index}]"
    if not isinstance(raw, dict):
        raise PromptSchemaError("expected a table", path=path)
    _reject_unknown_keys(raw, _SESSION_ALLOWED_KEYS | {"turns"}, path=path)

    sid = _require_str(raw.get("id"), path=f"{path}.id")
    if not sid.strip():
        raise PromptSchemaError("must be non-empty", path=f"{path}.id")

    source = _require_str(raw.get("source"), path=f"{path}.source")
    if source not in VALID_SOURCES:
        raise PromptSchemaError(
            f"unknown source `{source}`. Valid: {sorted(VALID_SOURCES)}",
            path=f"{path}.source",
        )

    outcome = _optional_str(raw.get("outcome"), path=f"{path}.outcome")
    if outcome is not None and outcome not in VALID_OUTCOMES:
        raise PromptSchemaError(
            f"unknown outcome `{outcome}`. Valid: {sorted(VALID_OUTCOMES)}",
            path=f"{path}.outcome",
        )

    visibility_raw = raw.get("visibility")
    if visibility_raw is None:
        visibility = DEFAULT_VISIBILITY
    else:
        visibility = _require_str(visibility_raw, path=f"{path}.visibility")
        if visibility not in VALID_VISIBILITIES:
            raise PromptSchemaError(
                f"unknown visibility `{visibility}`. Valid: {sorted(VALID_VISIBILITIES)}",
                path=f"{path}.visibility",
            )

    verbose_raw = raw.get("verbose", False)
    if not isinstance(verbose_raw, bool):
        raise PromptSchemaError("expected a boolean", path=f"{path}.verbose")

    merged_at_raw = raw.get("merged_at")
    merged_at = (
        _as_utc_datetime(merged_at_raw, path=f"{path}.merged_at")
        if merged_at_raw is not None
        else None
    )

    session = Session(
        id=sid,
        source=source,
        verbose=verbose_raw,
        started_at=(
            _as_utc_datetime(raw["started_at"], path=f"{path}.started_at")
            if raw.get("started_at") is not None
            else None
        ),
        ended_at=(
            _as_utc_datetime(raw["ended_at"], path=f"{path}.ended_at")
            if raw.get("ended_at") is not None
            else None
        ),
        model=_optional_str(raw.get("model"), path=f"{path}.model"),
        cwd=_optional_str(raw.get("cwd"), path=f"{path}.cwd"),
        operator=_optional_str(raw.get("operator"), path=f"{path}.operator"),
        title=_optional_str(raw.get("title"), path=f"{path}.title"),
        summary=_optional_str(raw.get("summary"), path=f"{path}.summary"),
        lesson=_optional_str(raw.get("lesson"), path=f"{path}.lesson"),
        tags=_optional_str_list(raw.get("tags"), path=f"{path}.tags"),
        outcome=outcome,
        visibility=visibility,
        forked_from=_optional_str(
            raw.get("forked_from"), path=f"{path}.forked_from"
        ),
        paths_touched=_optional_str_list(
            raw.get("paths_touched"), path=f"{path}.paths_touched"
        ),
        commit=_parse_session_commit(
            raw.get("commit"), path=f"{path}.commit"
        ),
        merged_from=_optional_str(
            raw.get("merged_from"), path=f"{path}.merged_from"
        ),
        merged_at=merged_at,
        approved_by=_optional_str(
            raw.get("approved_by"), path=f"{path}.approved_by"
        ),
    )
    session.turns = _parse_turns(
        raw.get("turns"), session_index=index, verbose=session.verbose
    )
    return session


def _parse_session_commit(raw: Any, *, path: str) -> SessionCommit | None:
    """Parse the optional `[sessions.commit]` block on a session.

    Every field is nullable — a session can carry just `commit_sha`
    when its branch identity comes from the file-level `[commit]`,
    or omit `[sessions.commit]` entirely on legacy single-commit
    files.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PromptSchemaError("expected a table", path=path)
    _reject_unknown_keys(raw, _SESSION_COMMIT_ALLOWED_KEYS, path=path)
    committed_at_raw = raw.get("committed_at")
    committed_at = (
        _as_utc_datetime(committed_at_raw, path=f"{path}.committed_at")
        if committed_at_raw is not None
        else None
    )
    return SessionCommit(
        branch=_optional_str(raw.get("branch"), path=f"{path}.branch"),
        commit_sha=_optional_str(raw.get("commit_sha"), path=f"{path}.commit_sha"),
        message=_optional_str(raw.get("message"), path=f"{path}.message"),
        committed_at=committed_at,
        author_name=_optional_str(
            raw.get("author_name"), path=f"{path}.author_name"
        ),
        author_email=_optional_str(
            raw.get("author_email"), path=f"{path}.author_email"
        ),
        author_username=_optional_str(
            raw.get("author_username"), path=f"{path}.author_username"
        ),
    )


def _parse_edits(raw: Any) -> list[PromptsEdit]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PromptSchemaError("expected a list of tables", path="edits")
    out: list[PromptsEdit] = []
    for i, item in enumerate(raw):
        out.append(_parse_edit(item, path=f"edits[{i}]"))
    return out


def _parse_edit(raw: Any, *, path: str) -> PromptsEdit:
    if not isinstance(raw, dict):
        raise PromptSchemaError("expected a table", path=path)
    _reject_unknown_keys(raw, _EDIT_ALLOWED_KEYS, path=path)
    at_val = raw.get("at")
    if at_val is None:
        raise PromptSchemaError("missing `at`", path=path)

    sessions_raw = raw.get("sessions") or []
    if not isinstance(sessions_raw, list):
        raise PromptSchemaError("expected a list of strings", path=f"{path}.sessions")
    sessions: list[str] = []
    for i, v in enumerate(sessions_raw):
        if not isinstance(v, str):
            raise PromptSchemaError(
                "expected a string", path=f"{path}.sessions[{i}]"
            )
        sessions.append(v)

    turns_raw = raw.get("turns") or []
    if not isinstance(turns_raw, list):
        raise PromptSchemaError("expected a list of integers", path=f"{path}.turns")
    turns: list[int] = []
    for i, v in enumerate(turns_raw):
        # bool is a subclass of int — reject it explicitly.
        if not isinstance(v, int) or isinstance(v, bool):
            raise PromptSchemaError("expected an integer", path=f"{path}.turns[{i}]")
        turns.append(v)

    return PromptsEdit(
        at=_as_utc_datetime(at_val, path=f"{path}.at"),
        by=_require_str(raw.get("by"), path=f"{path}.by"),
        sessions=sessions,
        turns=turns,
        reason=_optional_str(raw.get("reason"), path=f"{path}.reason"),
    )


def _parse_turns(raw: Any, *, session_index: int, verbose: bool) -> list[Turn]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PromptSchemaError(
            "expected an array of tables", path=f"sessions[{session_index}].turns"
        )
    out: list[Turn] = []
    for i, item in enumerate(raw):
        out.append(_parse_turn(item, session_index=session_index, turn_index=i, verbose=verbose))
    return out


def _parse_turn(raw: Any, *, session_index: int, turn_index: int, verbose: bool) -> Turn:
    path = f"sessions[{session_index}].turns[{turn_index}]"
    if not isinstance(raw, dict):
        raise PromptSchemaError("expected a table", path=path)
    _reject_unknown_keys(raw, _TURN_ALLOWED_KEYS, path=path)

    role = _require_str(raw.get("role"), path=f"{path}.role")
    if role in RESERVED_ROLES:
        raise PromptSchemaError(
            f"role `{role}` is reserved and not allowed in v0.1", path=f"{path}.role"
        )
    if role not in VALID_ROLES:
        raise PromptSchemaError(
            f"unknown role `{role}`. Valid: {sorted(VALID_ROLES)}", path=f"{path}.role"
        )

    text = raw.get("text")
    summary = raw.get("summary")
    at = (
        _as_utc_datetime(raw["at"], path=f"{path}.at")
        if raw.get("at") is not None
        else None
    )

    if role == "user":
        if text is None:
            raise PromptSchemaError("user turn requires `text`", path=path)
        if not isinstance(text, str):
            raise PromptSchemaError("expected a string", path=f"{path}.text")
        if len(text) > MAX_TURN_TEXT_CHARS:
            raise PromptSchemaError(
                f"text exceeds {MAX_TURN_TEXT_CHARS} chars — split or trim the turn.",
                path=f"{path}.text",
            )
        if summary is not None:
            raise PromptSchemaError(
                "user turns carry `text`, not `summary`", path=f"{path}.summary"
            )
    else:  # assistant
        if text is not None and not verbose:
            raise PromptSchemaError(
                "assistant turns must not carry `text` unless session.verbose = true. "
                "Use `summary` for short descriptions; regenerate full text with "
                "`spec prompts simulate`.",
                path=f"{path}.text",
            )
        if text is not None and not isinstance(text, str):
            raise PromptSchemaError("expected a string", path=f"{path}.text")
        if (
            text is not None
            and verbose
            and len(text) > MAX_TURN_TEXT_CHARS
        ):
            raise PromptSchemaError(
                f"text exceeds {MAX_TURN_TEXT_CHARS} chars — split or trim the turn.",
                path=f"{path}.text",
            )
        if summary is not None and not isinstance(summary, str):
            raise PromptSchemaError("expected a string", path=f"{path}.summary")

    tool_calls = _parse_tool_calls(raw.get("tool_calls"), turn_path=path)

    return Turn(role=role, text=text, summary=summary, at=at, tool_calls=tool_calls)


def _parse_tool_calls(raw: Any, *, turn_path: str) -> list[ToolCall]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PromptSchemaError(
            "expected an array of tables", path=f"{turn_path}.tool_calls"
        )
    out: list[ToolCall] = []
    for i, item in enumerate(raw):
        path = f"{turn_path}.tool_calls[{i}]"
        if not isinstance(item, dict):
            raise PromptSchemaError("expected a table", path=path)
        _reject_unknown_keys(item, _TOOL_CALL_ALLOWED_KEYS, path=path)
        name = _require_str(item.get("name"), path=f"{path}.name")
        if name not in ALLOWED_TOOL_NAMES:
            # Not fatal — `prompts validate --strict-unknown` upgrades this
            # to an error. Captured files are sanitized by the source
            # adapter, so a rogue name here means a hand-edit. We keep the
            # record rather than silently dropping.
            pass
        args = item.get("args") or {}
        if not isinstance(args, dict):
            raise PromptSchemaError("expected a table", path=f"{path}.args")
        status = item.get("status")
        if status is not None:
            if not isinstance(status, str):
                raise PromptSchemaError("expected a string", path=f"{path}.status")
            if status not in {"ok", "error"}:
                raise PromptSchemaError(
                    f"unknown status `{status}` — expected `ok` or `error`",
                    path=f"{path}.status",
                )
        out.append(ToolCall(name=name, args=dict(args), status=status))
    return out


# ---------------------------------------------------------------------------
# Re-validation (for in-memory objects, before render)
# ---------------------------------------------------------------------------


def validate_session(session: Session, *, path: str = "session") -> None:
    """Re-run the schema's structural rules against an in-memory Session."""
    if not session.id or not session.id.strip():
        raise PromptSchemaError("must be non-empty", path=f"{path}.id")
    if session.source not in VALID_SOURCES:
        raise PromptSchemaError(
            f"unknown source `{session.source}`. Valid: {sorted(VALID_SOURCES)}",
            path=f"{path}.source",
        )
    if session.outcome is not None and session.outcome not in VALID_OUTCOMES:
        raise PromptSchemaError(
            f"unknown outcome `{session.outcome}`", path=f"{path}.outcome"
        )
    if session.visibility not in VALID_VISIBILITIES:
        raise PromptSchemaError(
            f"unknown visibility `{session.visibility}`", path=f"{path}.visibility"
        )
    if not session.turns:
        raise PromptSchemaError("turns must not be empty", path=f"{path}.turns")

    for i, t in enumerate(session.turns):
        tpath = f"{path}.turns[{i}]"
        if t.role in RESERVED_ROLES:
            raise PromptSchemaError(
                f"role `{t.role}` is reserved", path=f"{tpath}.role"
            )
        if t.role not in VALID_ROLES:
            raise PromptSchemaError(f"unknown role `{t.role}`", path=f"{tpath}.role")
        if t.role == "user":
            if not t.text:
                raise PromptSchemaError("user turn requires `text`", path=tpath)
            if len(t.text) > MAX_TURN_TEXT_CHARS:
                raise PromptSchemaError(
                    f"text exceeds {MAX_TURN_TEXT_CHARS} chars", path=f"{tpath}.text"
                )
            if t.summary is not None:
                raise PromptSchemaError(
                    "user turns carry `text`, not `summary`",
                    path=f"{tpath}.summary",
                )
        else:
            if t.text is not None and not session.verbose:
                raise PromptSchemaError(
                    "assistant turn carries `text` but session.verbose is false",
                    path=f"{tpath}.text",
                )
            if (
                t.text is not None
                and session.verbose
                and len(t.text) > MAX_TURN_TEXT_CHARS
            ):
                raise PromptSchemaError(
                    f"text exceeds {MAX_TURN_TEXT_CHARS} chars", path=f"{tpath}.text"
                )

        for j, call in enumerate(t.tool_calls):
            cpath = f"{tpath}.tool_calls[{j}]"
            if not call.name:
                raise PromptSchemaError("must be non-empty", path=f"{cpath}.name")
            if not isinstance(call.args, dict):
                raise PromptSchemaError("must be a table", path=f"{cpath}.args")
            # Sanity: if the tool is on the allowlist, re-summarize the
            # current args to confirm the writer/adapter didn't let a
            # payload slip through. We compare key-sets rather than values
            # so hand-edits to allowed fields don't falsely trip this.
            if call.name in ALLOWED_TOOL_NAMES:
                expected = summarize_tool_call(call.name, call.args) or {}
                extra = set(call.args) - set(expected)
                if extra:
                    raise PromptSchemaError(
                        f"tool `{call.name}` has unexpected arg keys: {sorted(extra)}",
                        path=f"{cpath}.args",
                    )


def validate_commit(commit: CommitMeta, *, path: str = "commit") -> None:
    if not commit.branch or not commit.branch.strip():
        raise PromptSchemaError("must be non-empty", path=f"{path}.branch")
    if not commit.author_name or not commit.author_name.strip():
        raise PromptSchemaError("must be non-empty", path=f"{path}.author_name")
    if not commit.author_email or not commit.author_email.strip():
        raise PromptSchemaError("must be non-empty", path=f"{path}.author_email")


def validate_prompts_file(pf: PromptsFile) -> None:
    """Re-run all structural rules against an in-memory PromptsFile. Adapters
    and the renderer call this so we never emit an invalid file."""
    validate_commit(pf.commit)

    if not pf.sessions:
        raise PromptSchemaError(
            "at least one [[sessions]] block is required", path="sessions"
        )

    seen_ids: set[str] = set()
    for i, s in enumerate(pf.sessions):
        if s.id in seen_ids:
            raise PromptSchemaError(
                f"duplicate session id `{s.id}` — each session must be unique within a .prompts file",
                path=f"sessions[{i}].id",
            )
        seen_ids.add(s.id)
        validate_session(s, path=f"sessions[{i}]")


# ---------------------------------------------------------------------------
# Public re-exports (back-compat friendly names)
# ---------------------------------------------------------------------------

# The old singular `parse_prompt_text` API is intentionally removed; callers
# use `parse_prompts_text` / `read_prompts_file`.

__all__ = [
    "SCHEMA_VERSION",
    "VALID_SOURCES",
    "VALID_ROLES",
    "VALID_OUTCOMES",
    "VALID_VISIBILITIES",
    "DEFAULT_VISIBILITY",
    "MAX_TURN_TEXT_CHARS",
    "MAX_USER_TEXT_CHARS",
    "PromptSchemaError",
    "ToolCall",
    "Turn",
    "Session",
    "SessionCommit",
    "CommitMeta",
    "PromptsEdit",
    "PromptsFile",
    "parse_prompts_text",
    "read_prompts_file",
    "validate_session",
    "validate_commit",
    "validate_prompts_file",
]
