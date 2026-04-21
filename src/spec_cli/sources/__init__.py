"""
Adapters that read captured conversational sessions from coding-agent clients
and normalize them into `spec_cli.prompts.Session` objects.

Only Claude Code is supported in v0.1. The `.prompt` format's `source` enum
reserves `"cursor"` for a future adapter — see `docs/prompt-format.md`.
"""

from .claude_code import (
    ClaudeCodeError,
    claude_code_project_dir,
    claude_code_store_root,
    encode_bundle_path,
    read_claude_code_sessions,
)

__all__ = [
    "ClaudeCodeError",
    "claude_code_project_dir",
    "claude_code_store_root",
    "encode_bundle_path",
    "read_claude_code_sessions",
]
