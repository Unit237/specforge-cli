"""
Adapters that read captured conversational sessions from coding-agent clients
and normalize them into `spec_cli.prompts.Session` objects.

Two adapters today:

  - ``claude_code`` — reads Claude Code's per-project JSONL store.
  - ``cursor``      — reads Cursor's per-workspace SQLite store.

Both adapters expose a ``read_*_sessions(bundle_paths, *, since,
verbose)`` entry point. ``bundle_paths`` may be a single ``Path`` or
an iterable of paths (current root + historical aliases — see
``stage.historical_bundle_paths`` for Fix #2). Each adapter handles
its own client's idiosyncratic on-disk format; the rest of the CLI
treats a ``Session`` as a Session regardless of source.
"""

from .claude_code import (
    ClaudeCodeError,
    claude_code_project_dir,
    claude_code_store_root,
    encode_bundle_path,
    read_claude_code_sessions,
)
from .cursor import (
    CursorError,
    cursor_global_storage_db,
    cursor_workspace_storage_root,
    read_cursor_sessions,
)

__all__ = [
    "ClaudeCodeError",
    "CursorError",
    "claude_code_project_dir",
    "claude_code_store_root",
    "cursor_global_storage_db",
    "cursor_workspace_storage_root",
    "encode_bundle_path",
    "read_claude_code_sessions",
    "read_cursor_sessions",
]
