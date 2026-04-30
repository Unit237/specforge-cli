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


def test_read_sessions_strips_ansi_from_captured_text(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    session_id = "ansi-session"
    raw_user = "Run tests \x1b[32mok\x1b[0m please"
    rows = [
        {
            "type": "user",
            "cwd": str(bundle.resolve()),
            "sessionId": session_id,
            "timestamp": "2026-03-10T11:00:00Z",
            "message": {"role": "user", "content": raw_user},
        },
    ]
    _make_fake_store(tmp_path, bundle, session_id, rows)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    sessions = list(read_claude_code_sessions(bundle))
    assert len(sessions) == 1
    assert "\x1b" not in (sessions[0].turns[0].text or "")


def test_read_sessions_returns_empty_when_store_missing(tmp_path, monkeypatch):
    bundle = tmp_path / "no-store-bundle"
    bundle.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "does-not-exist"))
    assert list(read_claude_code_sessions(bundle)) == []


# ---------------------------------------------------------------------------
# Bundle-scoping: prompt visibility mirrors git's worktree boundary.
# ---------------------------------------------------------------------------


def test_read_sessions_includes_subdirectory_sessions(tmp_path, monkeypatch):
    """A session started in a subfolder of the bundle should be picked up.

    Mirrors git: a `git commit` from `<repo>/backend/` is part of the
    repo. We do the same for `claude` invocations from `<bundle>/backend/`.
    """
    bundle = tmp_path / "bundle"
    sub = bundle / "backend"
    sub.mkdir(parents=True)

    # Sessions in the bundle root proper.
    root_session = "11111111-1111-4111-a111-111111111111"
    _make_fake_store(
        tmp_path,
        bundle,
        root_session,
        [
            {
                "type": "user",
                "cwd": str(bundle.resolve()),
                "sessionId": root_session,
                "timestamp": "2026-03-10T11:00:00Z",
                "message": {"role": "user", "content": "root work"},
            }
        ],
    )

    # Session started one level deeper. Claude Code stores it in a
    # sibling directory whose encoded name is the bundle's encoding +
    # `-backend`.
    sub_session = "22222222-2222-4222-a222-222222222222"
    _make_fake_store(
        tmp_path,
        sub,
        sub_session,
        [
            {
                "type": "user",
                "cwd": str(sub.resolve()),
                "sessionId": sub_session,
                "timestamp": "2026-03-10T12:00:00Z",
                "message": {"role": "user", "content": "subdir work"},
            }
        ],
    )

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))

    sessions = list(read_claude_code_sessions(bundle))
    ids = sorted(s.id for s in sessions)
    assert ids == sorted([root_session, sub_session])


def test_read_sessions_rejects_lossy_encoding_collisions(tmp_path, monkeypatch):
    """Encoded `<root>-foo` could mean `<root>/foo` OR sibling `<root>-foo`.

    Claude Code's path encoding replaces both `/` and `-` with `-`, so
    a sibling repo at `bundle-old` can't be told apart from a subfolder
    at `bundle/old` by directory name alone. The cwd check has to be
    the source of truth.
    """
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    # `bundle-old` is a *sibling*, not a subfolder. Sessions in it
    # must be excluded from `bundle`'s prompt scope.
    sibling = tmp_path / "bundle-old"
    sibling.mkdir()

    sibling_session = "ffffffff-ffff-4fff-afff-ffffffffffff"
    _make_fake_store(
        tmp_path,
        sibling,
        sibling_session,
        [
            {
                "type": "user",
                "cwd": str(sibling.resolve()),
                "sessionId": sibling_session,
                "timestamp": "2026-03-10T13:00:00Z",
                "message": {"role": "user", "content": "in the wrong repo"},
            }
        ],
    )

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))

    sessions = list(read_claude_code_sessions(bundle))
    assert sessions == []


def test_read_sessions_drops_session_with_outside_cwd(tmp_path, monkeypatch):
    """Defense-in-depth: even inside the exact-encoded directory, a
    session whose `cwd` row points outside the bundle is dropped.

    This guards against a future Claude Code change (or a corrupted
    store) where the directory name and the recorded cwd diverge.
    """
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    other = tmp_path / "other"
    other.mkdir()

    session_id = "deadbeef-dead-4eef-aeef-deadbeefdead"
    _make_fake_store(
        tmp_path,
        bundle,  # file lives in bundle's encoded dir
        session_id,
        [
            {
                "type": "user",
                "cwd": str(other.resolve()),  # …but cwd row claims another tree
                "sessionId": session_id,
                "timestamp": "2026-03-10T14:00:00Z",
                "message": {"role": "user", "content": "stray"},
            }
        ],
    )

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))

    sessions = list(read_claude_code_sessions(bundle))
    assert sessions == []


def test_read_sessions_finds_sessions_under_historical_alias(tmp_path, monkeypatch):
    """Fix #2 (rename): a list of bundle paths makes prompt history
    survive a folder move.

    We simulate the rename by writing a Claude Code session under the
    *old* path, then asking the adapter to read with both paths in the
    aliases list. The session should come back exactly once.
    """
    old_path = tmp_path / "billing"
    new_path = tmp_path / "payments"
    old_path.mkdir()
    new_path.mkdir()

    session_id = "55555555-5555-4555-a555-555555555555"
    _make_fake_store(
        tmp_path,
        old_path,  # session was captured under the old folder name
        session_id,
        [
            {
                "type": "user",
                "cwd": str(old_path.resolve()),
                "sessionId": session_id,
                "timestamp": "2026-03-10T10:00:00Z",
                "message": {"role": "user", "content": "before the rename"},
            }
        ],
    )

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))

    # Reading with only the new path: nothing comes back, because the
    # store still lives under the old encoded directory name.
    assert list(read_claude_code_sessions(new_path)) == []

    # Reading with both paths (the rename-aware view) finds it once.
    sessions = list(read_claude_code_sessions([new_path, old_path]))
    assert len(sessions) == 1
    assert sessions[0].id == session_id


def test_read_sessions_dedupes_across_alias_paths(tmp_path, monkeypatch):
    """A session that somehow exists under *both* historical paths must
    only be yielded once.

    Edge case in practice — happens when Claude Code keeps a session
    open across a directory rename — but the adapter should be safe
    either way.
    """
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    sid = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
    for path in (a, b):
        _make_fake_store(
            tmp_path,
            path,
            sid,
            [
                {
                    "type": "user",
                    "cwd": str(path.resolve()),
                    "sessionId": sid,
                    "timestamp": "2026-03-10T11:00:00Z",
                    "message": {"role": "user", "content": "hi"},
                }
            ],
        )

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))

    sessions = list(read_claude_code_sessions([a, b]))
    assert len(sessions) == 1
    assert sessions[0].id == sid


def test_read_sessions_keeps_session_without_cwd_in_exact_dir(tmp_path, monkeypatch):
    """Backward-compat: the exact-encoded directory accepts sessions
    even when no row carries a `cwd` field — older Claude Code captures
    sometimes don't. (Subfolder candidates require cwd; that's covered
    by `test_read_sessions_rejects_lossy_encoding_collisions`.)"""
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    session_id = "12345678-1234-4234-a234-123456789012"
    _make_fake_store(
        tmp_path,
        bundle,
        session_id,
        [
            {
                "type": "user",
                # no `cwd` key
                "sessionId": session_id,
                "timestamp": "2026-03-10T15:00:00Z",
                "message": {"role": "user", "content": "legacy"},
            }
        ],
    )

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))

    sessions = list(read_claude_code_sessions(bundle))
    assert len(sessions) == 1
    assert sessions[0].id == session_id
