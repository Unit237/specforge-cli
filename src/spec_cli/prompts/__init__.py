"""
The `.prompts` file format — schema, parser, renderer, tool-call allowlist.

See `docs/prompt-format.md` for the authoritative spec. Everything in this
package exists to read, validate, and write that format deterministically.
"""

from .schema import (
    CommitMeta,
    PromptSchemaError,
    PromptsEdit,
    PromptsFile,
    Session,
    ToolCall,
    Turn,
    parse_prompts_text,
    read_prompts_file,
    validate_commit,
    validate_prompts_file,
    validate_session,
)

__all__ = [
    "CommitMeta",
    "PromptSchemaError",
    "PromptsEdit",
    "PromptsFile",
    "Session",
    "ToolCall",
    "Turn",
    "parse_prompts_text",
    "read_prompts_file",
    "validate_commit",
    "validate_prompts_file",
    "validate_session",
]
