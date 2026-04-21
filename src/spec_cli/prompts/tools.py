"""
Tool-call allowlist, per-tool argument summarizers, and secret scrub.

The rules enforced here are the same for every source adapter: whether a
call was captured from Claude Code, Cursor (future), or hand-authored, the
args stored in a `.prompt` file must match what this module produces for
the given tool name. This is what makes the format deterministic enough
for the compiler to reason over.

Principles:
  - Capture the *call*, never the *payload*. We store tool names and
    small-bounded arg summaries, never file contents, command output,
    diffs, or grep matches.
  - Truncate every string field. A 32 KiB shell command line is not
    useful in an audit.
  - Scrub obvious secrets. Defense in depth — users may paste tokens
    into shell commands and we should not help them commit those.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------

# Per-string hard cap. `command` + `url` + free-text arg values get the
# long cap; `old_head` / `new_head` get the short cap so Edit summaries
# stay dense.
MAX_LONG_CHARS: int = 500
MAX_SHORT_CHARS: int = 40
MAX_COMMAND_CHARS: int = 200


# ---------------------------------------------------------------------------
# Secret scrub
# ---------------------------------------------------------------------------

# Patterns that strongly indicate a secret in a free-text arg. Order
# matters — longer / more specific patterns first so a bearer token
# doesn't get half-matched as a JWT or vice versa.
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    # "Authorization: Bearer <token>" or "Bearer <token>"
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}"),
    re.compile(r"(?i)\bauthorization\s*:\s*[A-Za-z0-9._~+/=\-]{8,}"),
    # OpenAI-style
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    # GitHub personal access tokens / app tokens
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bghs_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgho_[A-Za-z0-9]{20,}"),
    # AWS access key id
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # Anthropic
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"),
    # JWTs (three dot-separated base64url chunks, first starts with eyJ)
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    # Generic "password=..." or "token=..." in a URL/query/env-like string
    re.compile(r"(?i)\b(password|passwd|token|secret|api[_-]?key)\s*[=:]\s*[^\s&\"']{8,}"),
]

_REDACTED = "[REDACTED]"


def scrub_secrets(s: str) -> str:
    """Redact substrings that look like credentials. Best-effort — this
    is not a security control, it's a hygiene pass. Don't trust it with
    anything irrevocable."""
    if not s:
        return s
    out = s
    for pat in _SECRET_PATTERNS:
        out = pat.sub(_REDACTED, out)
    return out


# ---------------------------------------------------------------------------
# String normalization
# ---------------------------------------------------------------------------


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f"…[truncated {len(s) - n} chars]"


def _scrub_and_truncate(s: Any, n: int) -> str:
    if not isinstance(s, str):
        s = "" if s is None else str(s)
    return _truncate(scrub_secrets(s), n)


# ---------------------------------------------------------------------------
# Per-tool summarizers
# ---------------------------------------------------------------------------

# Each summarizer takes the raw `input` dict the source emitted and returns
# the sanitized `args` we'll store in the .prompt file. Return None to drop
# the call entirely (e.g. malformed input we can't safely represent).

def _s_read(inp: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"path": _scrub_and_truncate(inp.get("path", ""), MAX_LONG_CHARS)}
    if "offset" in inp:
        try:
            out["offset"] = int(inp["offset"])
        except (TypeError, ValueError):
            pass
    if "limit" in inp:
        try:
            out["limit"] = int(inp["limit"])
        except (TypeError, ValueError):
            pass
    return out


def _s_glob(inp: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "pattern": _scrub_and_truncate(
            inp.get("pattern") or inp.get("glob_pattern") or "", MAX_LONG_CHARS
        )
    }
    target = inp.get("target_directory") or inp.get("path")
    if target:
        out["target_directory"] = _scrub_and_truncate(target, MAX_LONG_CHARS)
    return out


def _s_grep(inp: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "pattern": _scrub_and_truncate(inp.get("pattern", ""), MAX_LONG_CHARS)
    }
    for key in ("path", "glob", "type", "output_mode"):
        v = inp.get(key)
        if v:
            out[key] = _scrub_and_truncate(v, MAX_LONG_CHARS)
    return out


def _s_shell(inp: dict[str, Any]) -> dict[str, Any]:
    cmd = inp.get("command") or inp.get("cmd") or ""
    out: dict[str, Any] = {"command": _scrub_and_truncate(cmd, MAX_COMMAND_CHARS)}
    cwd = inp.get("working_directory") or inp.get("cwd")
    if cwd:
        out["cwd"] = _scrub_and_truncate(cwd, MAX_LONG_CHARS)
    # `exit` (int) is optional — sources that don't know the code just
    # don't include it.
    exit_code = inp.get("exit") or inp.get("exit_code")
    if exit_code is not None:
        try:
            out["exit"] = int(exit_code)
        except (TypeError, ValueError):
            pass
    return out


def _s_write(inp: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"path": _scrub_and_truncate(inp.get("path", ""), MAX_LONG_CHARS)}
    content = inp.get("contents") or inp.get("content")
    if isinstance(content, str):
        out["bytes"] = len(content.encode("utf-8"))
    elif "bytes" in inp:
        try:
            out["bytes"] = int(inp["bytes"])
        except (TypeError, ValueError):
            pass
    return out


def _s_edit(inp: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"path": _scrub_and_truncate(inp.get("path", ""), MAX_LONG_CHARS)}
    old = inp.get("old_string") or inp.get("old") or inp.get("old_head") or ""
    new = inp.get("new_string") or inp.get("new") or inp.get("new_head") or ""
    # First line only, then truncated, so a huge edit summary stays dense.
    old_head = old.splitlines()[0] if old else ""
    new_head = new.splitlines()[0] if new else ""
    out["old_head"] = _scrub_and_truncate(old_head, MAX_SHORT_CHARS)
    out["new_head"] = _scrub_and_truncate(new_head, MAX_SHORT_CHARS)
    return out


def _s_delete(inp: dict[str, Any]) -> dict[str, Any]:
    return {"path": _scrub_and_truncate(inp.get("path", ""), MAX_LONG_CHARS)}


def _s_webfetch(inp: dict[str, Any]) -> dict[str, Any]:
    return {"url": _scrub_and_truncate(inp.get("url", ""), MAX_LONG_CHARS)}


def _s_websearch(inp: dict[str, Any]) -> dict[str, Any]:
    term = inp.get("search_term") or inp.get("query") or inp.get("q") or ""
    return {"search_term": _scrub_and_truncate(term, MAX_LONG_CHARS)}


def _s_task(inp: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "subagent_type": _scrub_and_truncate(inp.get("subagent_type", ""), MAX_SHORT_CHARS),
        "description": _scrub_and_truncate(inp.get("description", ""), MAX_LONG_CHARS),
    }
    model = inp.get("model")
    if model:
        out["model"] = _scrub_and_truncate(model, MAX_SHORT_CHARS)
    return out


def _s_todowrite(inp: dict[str, Any]) -> dict[str, Any]:
    # Passed through; the structure is small and already normalized by the
    # tool's own schema. We still scrub + truncate each content string.
    todos = inp.get("todos") or []
    out_todos: list[dict[str, Any]] = []
    if isinstance(todos, list):
        for t in todos:
            if not isinstance(t, dict):
                continue
            out_todos.append(
                {
                    "id": _scrub_and_truncate(t.get("id", ""), MAX_SHORT_CHARS),
                    "content": _scrub_and_truncate(t.get("content", ""), MAX_LONG_CHARS),
                    "status": _scrub_and_truncate(t.get("status", "pending"), MAX_SHORT_CHARS),
                }
            )
    return {"todos": out_todos}


# The allowlist. A tool not listed here is dropped with a warning by the
# source adapter (§tool-calls, rule 4 in docs/prompt-format.md).
_SUMMARIZERS = {
    "Read": _s_read,
    "Glob": _s_glob,
    "Grep": _s_grep,
    "Shell": _s_shell,
    "Bash": _s_shell,  # Claude Code historically called this tool `Bash`.
    "Write": _s_write,
    "Edit": _s_edit,
    "StrReplace": _s_edit,
    "Delete": _s_delete,
    "WebFetch": _s_webfetch,
    "WebSearch": _s_websearch,
    "Task": _s_task,
    "TodoWrite": _s_todowrite,
}

ALLOWED_TOOL_NAMES: frozenset[str] = frozenset(_SUMMARIZERS.keys())


def summarize_tool_call(name: str, raw_input: Any) -> dict[str, Any] | None:
    """
    Produce the sanitized `args` dict for a tool call. Returns None if the
    tool isn't on the allowlist — caller should drop + record a warning.

    `raw_input` may be any type; we coerce to dict and pick only documented
    fields.
    """
    summarizer = _SUMMARIZERS.get(name)
    if summarizer is None:
        return None
    if not isinstance(raw_input, dict):
        raw_input = {}
    try:
        return summarizer(raw_input)
    except Exception:  # noqa: BLE001
        # A malformed input should drop the call, not crash sync. The
        # source adapter's own warning path handles the user-visible
        # message.
        return None
