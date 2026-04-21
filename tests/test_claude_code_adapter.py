"""Tests for the Claude Code adapter.

We fabricate a Claude Code project store in a tmp dir and point
`CLAUDE_HOME` at it, so the tests don't depend on the developer's own
Claude Code history.
"""

from __future__ import annotations

import json
from pathlib import Path

from spec_cli.sources.claude_code import (
    encode_bundle_path,
    read_claude_code_sessions,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _make_fake_store(
    tmp_path: Path, bundle_root: Path, session_id: str, rows: list[dict]
) -> Path:
    """Create ~/.claude/projects/<encoded>/<session>.jsonl under tmp_path."""
    encoded = encode_bundle_path(bundle_root)
    jsonl_path = tmp_path / "projects" / encoded / f"{session_id}.jsonl"
    _write_jsonl(jsonl_path, rows)
    return jsonl_path


def test_encode_bundle_path_matches_claude_convention(tmp_path, monkeypatch):
    # Claude Code encodes `/Users/foo/bar` as `-Users-foo-bar`.
    p = Path("/Users/foo/bar")
    assert encode_bundle_path(p) == "-Users-foo-bar"


def test_read_sessions_extracts_user_and_assistant_turns(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    session_id = "d1714569-2799-464b-9a0e-360aced5767c"
    rows = [
        {
            "type": "user",
            "cwd": str(bundle.resolve()),
            "sessionId": session_id,
            "gitBranch": "main",
            "timestamp": "2026-03-10T11:47:12.717Z",
            "message": {"role": "user", "content": "Refactor billing.py please."},
        },
        {
            "type": "assistant",
            "cwd": str(bundle.resolve()),
            "sessionId": session_id,
            "gitBranch": "main",
            "timestamp": "2026-03-10T11:47:35.000Z",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [
                    {"type": "text", "text": "Scanning tax call sites. Quick look."},
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "Grep",
                        "input": {"pattern": "calculate_tax", "path": "billing/"},
                    },
                ],
            },
        },
    ]
    _make_fake_store(tmp_path, bundle, session_id, rows)

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))

    sessions = list(read_claude_code_sessions(bundle))
    assert len(sessions) == 1
    s = sessions[0]
    assert s.id == session_id
    assert s.source == "claude_code"
    assert s.model == "claude-sonnet-4-5"
    assert len(s.turns) == 2

    # Tool-call args with `path` populate the session's paths_touched list,
    # which is how the feed shows "which files did this session affect".
    assert s.paths_touched == ["billing/"]

    assert s.turns[0].role == "user"
    assert s.turns[0].text == "Refactor billing.py please."

    assert s.turns[1].role == "assistant"
    # Non-verbose mode: no text, just a bounded summary.
    assert s.turns[1].text is None
    assert s.turns[1].summary and "tax" in s.turns[1].summary.lower()
    assert len(s.turns[1].tool_calls) == 1
    call = s.turns[1].tool_calls[0]
    assert call.name == "Grep"
    assert call.args == {"pattern": "calculate_tax", "path": "billing/"}


def test_read_sessions_skips_sidechain_and_tool_result_rows(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    session_id = "abc-123"

    rows = [
        {
            "type": "user",
            "cwd": str(bundle.resolve()),
            "sessionId": session_id,
            "timestamp": "2026-03-10T11:00:00Z",
            "message": {"role": "user", "content": "hello"},
        },
        # Tool result flowing back into the model — not a user turn in our model.
        {
            "type": "user",
            "cwd": str(bundle.resolve()),
            "sessionId": session_id,
            "timestamp": "2026-03-10T11:00:05Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "x", "content": "42"}
                ],
            },
        },
        # Sidechain row — skipped wholesale in v0.1.
        {
            "type": "assistant",
            "isSidechain": True,
            "cwd": str(bundle.resolve()),
            "sessionId": session_id,
            "timestamp": "2026-03-10T11:00:10Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "side work"}],
            },
        },
        {
            "type": "assistant",
            "cwd": str(bundle.resolve()),
            "sessionId": session_id,
            "timestamp": "2026-03-10T11:00:20Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "done!"}],
            },
        },
    ]
    _make_fake_store(tmp_path, bundle, session_id, rows)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))

    sessions = list(read_claude_code_sessions(bundle))
    assert len(sessions) == 1
    assert len(sessions[0].turns) == 2  # user + final assistant only
    assert sessions[0].turns[0].role == "user"
    assert sessions[0].turns[1].role == "assistant"


def test_read_sessions_drops_unknown_tool_calls(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    session_id = "dropped-tool"

    rows = [
        {
            "type": "user",
            "cwd": str(bundle.resolve()),
            "sessionId": session_id,
            "timestamp": "2026-03-10T11:00:00Z",
            "message": {"role": "user", "content": "hi"},
        },
        {
            "type": "assistant",
            "cwd": str(bundle.resolve()),
            "sessionId": session_id,
            "timestamp": "2026-03-10T11:00:05Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "x", "name": "SomeFutureTool", "input": {}},
                    {"type": "text", "text": "trying a thing"},
                ],
            },
        },
    ]
    _make_fake_store(tmp_path, bundle, session_id, rows)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))

    sessions = list(read_claude_code_sessions(bundle))
    assert len(sessions) == 1
    assert len(sessions[0].turns[1].tool_calls) == 0


def test_read_sessions_verbose_captures_full_text(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    session_id = "verbose-1"

    rows = [
        {
            "type": "user",
            "cwd": str(bundle.resolve()),
            "sessionId": session_id,
            "timestamp": "2026-03-10T11:00:00Z",
            "message": {"role": "user", "content": "hi"},
        },
        {
            "type": "assistant",
            "cwd": str(bundle.resolve()),
            "sessionId": session_id,
            "timestamp": "2026-03-10T11:00:05Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "A longer assistant reply."}],
            },
        },
    ]
    _make_fake_store(tmp_path, bundle, session_id, rows)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))

    sessions = list(read_claude_code_sessions(bundle, verbose=True))
    assert sessions[0].verbose is True
    assert sessions[0].turns[1].text == "A longer assistant reply."


def test_read_sessions_returns_empty_when_store_missing(tmp_path, monkeypatch):
    bundle = tmp_path / "no-store-bundle"
    bundle.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "does-not-exist"))
    assert list(read_claude_code_sessions(bundle)) == []
