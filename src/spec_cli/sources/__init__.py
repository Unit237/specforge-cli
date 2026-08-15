"""
Adapters that read captured conversational sessions from coding-agent clients
and normalize them into `spec_cli.prompts.Session` objects.

Four adapters today:

  - ``claude_code`` — reads Claude Code's per-project JSONL store.
  - ``cursor``      — reads Cursor Composer (SQLite) plus in-editor Agent JSONL.
  - ``codex``       — reads OpenAI Codex Desktop rollout JSONL only.
  - ``compress``    — reads Compress terminal-agent session checkpoints.

All adapters expose a ``read_*_sessions(bundle_paths, *, since,
verbose)`` entry point. ``bundle_paths`` may be a single ``Path``, an
iterable of paths (current root + historical aliases — see
``stage.historical_bundle_paths`` for Fix #2), or ``None`` for the one
machine-wide live broadcaster elected by ``spec on``. Each adapter
handles its own client's idiosyncratic on-disk format; the rest of the
CLI treats a ``Session`` as a Session regardless of source.
"""

from .claude_code import (
    ClaudeCodeError,
    claude_code_project_dir,
    claude_code_store_root,
    encode_bundle_path,
    read_claude_code_sessions,
)
from .codex import (
    CodexRecentSession,
    CodexError,
    codex_desktop_index_path,
    list_recent_codex_sessions,
    codex_project_dir,
    codex_store_root,
    codex_transcript_store_available,
    iter_cursor_agent_transcript_sessions,
    read_codex_rollout_session,
    read_codex_sessions,
    redact_text,
)
from .cursor import (
    CursorError,
    cursor_global_storage_db,
    cursor_workspace_storage_root,
    read_cursor_sessions,
)
from .compress import CompressError, compress_session_store_root, read_compress_sessions

__all__ = [
    "ClaudeCodeError",
    "CodexError",
    "CodexRecentSession",
    "CursorError",
    "CompressError",
    "claude_code_project_dir",
    "claude_code_store_root",
    "codex_project_dir",
    "codex_desktop_index_path",
    "codex_store_root",
    "codex_transcript_store_available",
    "cursor_global_storage_db",
    "cursor_workspace_storage_root",
    "compress_session_store_root",
    "encode_bundle_path",
    "iter_cursor_agent_transcript_sessions",
    "list_recent_codex_sessions",
    "read_codex_rollout_session",
    "read_claude_code_sessions",
    "read_codex_sessions",
    "read_cursor_sessions",
    "read_compress_sessions",
    "redact_text",
]
