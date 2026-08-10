"""Adapter for Compress terminal-agent session checkpoints.

Compress writes one private JSON file per session under
``~/.compress/sessions`` (override with ``COMPRESS_SESSION_DIR``). The adapter
keeps only sessions whose recorded workspace intersects the current bundle and
normalizes their OpenAI-style messages into Spec's shared ``Session`` schema.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..bundle_scope import path_intersects_bundle
from ..prompts.schema import PromptSchemaError, Session, ToolCall, Turn, validate_session
from ..prompts.text_sanitize import sanitize_for_toml_text
from ..prompts.tools import ALLOWED_TOOL_NAMES, summarize_tool_call

_DEFAULT_COMPRESS_SESSION_DIR = "~/.compress/sessions"


class CompressError(RuntimeError):
    pass


def compress_session_store_root() -> Path:
    override = os.environ.get("COMPRESS_SESSION_DIR")
    return Path(override or _DEFAULT_COMPRESS_SESSION_DIR).expanduser()


def _parse_dt(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _assistant_turn(message: dict[str, Any], *, verbose: bool) -> Turn | None:
    content = message.get("content")
    text = sanitize_for_toml_text(content) if isinstance(content, str) else ""
    calls: list[ToolCall] = []
    for raw_call in message.get("tool_calls") or []:
        if not isinstance(raw_call, dict):
            continue
        function = raw_call.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or name not in ALLOWED_TOOL_NAMES:
            continue
        args = summarize_tool_call(name, _arguments(function.get("arguments")))
        if args is not None:
            calls.append(ToolCall(name=name, args=args))
    if not text.strip() and not calls:
        return None
    summary = " ".join(text.split())[:200] or None
    return Turn(
        role="assistant",
        text=text if verbose and text.strip() else None,
        summary=summary,
        tool_calls=calls,
    )


def _is_internal_user_message(content: str) -> bool:
    return content.startswith("Repository setup command `") or content.startswith(
        "Changes are staged but not verified."
    )


def _session_from_payload(
    payload: dict[str, Any],
    *,
    path: Path,
    roots: list[Path],
    verbose: bool,
) -> Session | None:
    session_id = payload.get("session_id")
    cwd = payload.get("cwd") or payload.get("workspace")
    messages = payload.get("messages")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    if not isinstance(cwd, str) or not Path(cwd).expanduser().is_absolute():
        return None
    workspace = Path(cwd).expanduser().resolve()
    if not any(path_intersects_bundle(workspace, root.resolve()) for root in roots):
        return None
    if not isinstance(messages, list):
        return None

    turns: list[Turn] = []
    paths_touched: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "user":
            content = message.get("content")
            if (
                not isinstance(content, str)
                or not content.strip()
                or _is_internal_user_message(content)
            ):
                continue
            turns.append(Turn(role="user", text=sanitize_for_toml_text(content).strip()))
        elif role == "assistant":
            turn = _assistant_turn(message, verbose=verbose)
            if turn is None:
                continue
            turns.append(turn)
            for call in turn.tool_calls:
                candidate = call.args.get("path")
                if isinstance(candidate, str) and candidate and candidate not in paths_touched:
                    paths_touched.append(candidate)
    if not turns or not any(turn.role == "user" for turn in turns):
        return None

    started = _parse_dt(payload.get("started_at"))
    updated = _parse_dt(payload.get("updated_at"))
    ended = _parse_dt(payload.get("ended_at"))
    if started is None:
        try:
            started = datetime.fromtimestamp(path.stat().st_ctime, tz=timezone.utc)
        except OSError:
            started = updated
    status = str(payload.get("status") or "").lower()
    if ended is None and status in {"completed", "failed", "cancelled", "timed_out"}:
        ended = updated
    first_user = next(turn.text for turn in turns if turn.role == "user" and turn.text)
    session = Session(
        id=session_id.strip(),
        source="compress",
        turns=turns,
        started_at=started,
        ended_at=ended,
        model=str(payload.get("model") or "").strip() or None,
        cwd=str(workspace),
        title=" ".join(first_user.split())[:200],
        paths_touched=paths_touched[:64],
        verbose=verbose,
    )
    validate_session(session)
    return session


def read_compress_sessions(
    bundle_paths: Path | Iterable[Path],
    *,
    since: datetime | None = None,
    verbose: bool = False,
) -> Iterable[Session]:
    roots = [bundle_paths] if isinstance(bundle_paths, Path) else list(bundle_paths)
    if not roots:
        return  # type: ignore[return-value]
    store = compress_session_store_root()
    if not store.is_dir():
        return  # type: ignore[return-value]
    try:
        paths = sorted(store.glob("*.json"))
    except OSError as exc:
        raise CompressError(f"Could not scan Compress session store: {exc}") from exc
    yielded: set[str] = set()
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        try:
            session = _session_from_payload(payload, path=path, roots=roots, verbose=verbose)
        except (OSError, PromptSchemaError, RuntimeError, TypeError, ValueError):
            continue
        if session is None or session.id in yielded:
            continue
        if since is not None and session.started_at is not None and session.started_at < since:
            continue
        yielded.add(session.id)
        yield session


__all__ = [
    "CompressError",
    "compress_session_store_root",
    "read_compress_sessions",
]
