"""Tests for the Codex transcript adapter."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from click.testing import CliRunner

from spec_cli.cli import cli
from spec_cli.prompts import read_prompts_file
from spec_cli.sources.codex import encode_bundle_path, read_codex_sessions
from spec_cli.sources.cursor import read_cursor_sessions
from spec_cli.sources.codex import (
    list_recent_codex_sessions,
    read_codex_rollout_session,
    redact_text,
)


def _write_transcript(
    codex_home: Path,
    bundle_root: Path,
    session_id: str,
    rows: list[dict],
) -> Path:
    encoded = encode_bundle_path(bundle_root)
    path = (
        codex_home
        / "projects"
        / encoded
        / "agent-transcripts"
        / session_id
        / f"{session_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return path


def test_encode_bundle_path_matches_cursor_project_naming() -> None:
    assert encode_bundle_path(Path("/Users/foo/bar")) == "Users-foo-bar"


def test_read_codex_sessions_extracts_turns(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    sid = "11111111-1111-4111-a111-111111111111"

    _write_transcript(
        tmp_path,
        bundle,
        sid,
        [
            {
                "role": "user",
                "message": {
                    "content": [{"type": "text", "text": "Refactor billing please."}]
                },
            },
            {
                "role": "assistant",
                "message": {
                    "content": [
                            {
                                "type": "text",
                                "text": "Mapping call sites first. Then I will refactor.",
                            }
                    ]
                },
            },
        ],
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    sessions = list(read_cursor_sessions(bundle))
    assert len(sessions) == 1
    s = sessions[0]
    assert s.id == sid
    assert s.source == "cursor"
    assert s.cwd == str(bundle.resolve())
    assert [t.role for t in s.turns] == ["user", "assistant"]
    assert s.turns[0].text == "Refactor billing please."
    assert s.turns[1].summary is not None
    assert "mapping call sites" in s.turns[1].summary.lower()
    assert s.turns[1].text is None


def test_agent_transcript_coalesces_redacted_assistant_steps(tmp_path, monkeypatch):
    """Cursor emits one JSONL row per tool step; many only have ``[REDACTED]`` prose."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    sid = "33333333-3333-4333-a333-333333333333"
    _write_transcript(
        tmp_path,
        bundle,
        sid,
        [
            {
                "role": "user",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": "<user_query>\nfix the feed\n</user_query>",
                        }
                    ]
                },
            },
            {
                "role": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "On it."},
                        {
                            "type": "tool_use",
                            "name": "Grep",
                            "input": {"pattern": "REDACTED", "path": str(bundle)},
                        },
                    ]
                },
            },
            {
                "role": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "[REDACTED]"},
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"path": str(bundle / "README.md")},
                        },
                    ]
                },
            },
            {
                "role": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "[REDACTED]"},
                        {
                            "type": "tool_use",
                            "name": "Glob",
                            "input": {"glob_pattern": "*.py", "target_directory": str(bundle)},
                        },
                    ]
                },
            },
        ],
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    sessions = list(read_cursor_sessions(bundle, verbose=True))
    assert len(sessions) == 1
    roles = [t.role for t in sessions[0].turns]
    assert roles == ["user", "assistant"]
    assert sessions[0].turns[0].text == "fix the feed"
    asst = sessions[0].turns[1]
    assert "On it." in (asst.text or "")
    assert len(asst.tool_calls or []) == 3
    assert {c.name for c in asst.tool_calls or []} == {"Grep", "Read", "Glob"}


def test_read_codex_sessions_verbose_keeps_assistant_text(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    sid = "22222222-2222-4222-a222-222222222222"
    _write_transcript(
        tmp_path,
        bundle,
        sid,
        [
            {
                "role": "user",
                "message": {"content": [{"type": "text", "text": "hello"}]},
            },
            {
                "role": "assistant",
                "message": {"content": [{"type": "text", "text": "A longer reply."}]},
            },
        ],
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    sessions = list(read_cursor_sessions(bundle, verbose=True))
    assert sessions[0].verbose is True
    assert sessions[0].turns[1].text == "A longer reply."


def test_read_codex_sessions_includes_subdirectory_aliases(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    sub = bundle / "backend"
    sub.mkdir(parents=True)
    root_id = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
    sub_id = "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"
    _write_transcript(
        tmp_path,
        bundle,
        root_id,
        [{"role": "user", "message": {"content": [{"type": "text", "text": "root"}]}}],
    )
    _write_transcript(
        tmp_path,
        sub,
        sub_id,
        [{"role": "user", "message": {"content": [{"type": "text", "text": "sub"}]}}],
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    sessions = list(read_cursor_sessions(bundle))
    assert sorted(s.id for s in sessions) == sorted([root_id, sub_id])


def test_read_codex_sessions_returns_empty_when_store_missing(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    assert list(read_codex_sessions(bundle)) == []


def test_read_codex_sessions_honors_cursor_home_fallback(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    sid = "cccccccc-cccc-4ccc-accc-cccccccccccc"
    _write_transcript(
        tmp_path,
        bundle,
        sid,
        [{"role": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}],
    )
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("CURSOR_HOME", str(tmp_path))
    sessions = list(read_cursor_sessions(bundle))
    assert [s.id for s in sessions] == [sid]


def _write_rollout(path: Path, *, sid: str, cwd: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "timestamp": "2026-05-08T08:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": sid,
                "cwd": str(cwd),
                "model": "gpt-5.5",
            },
        },
        {
            "timestamp": "2026-05-08T08:01:00Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "Capture this Codex chat. Authorization: Bearer secret-token",
            },
        },
        {
            "timestamp": "2026-05-08T08:02:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Done. I will write the prompts file.",
                    }
                ],
            },
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _write_codex_state(
    home: Path,
    *,
    sid: str,
    title: str,
    cwd: Path,
    rollout: Path,
    model: str = "gpt-5.5",
) -> None:
    db = home / "state_5.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            rollout_path TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            source TEXT NOT NULL,
            model_provider TEXT NOT NULL,
            cwd TEXT NOT NULL,
            title TEXT NOT NULL,
            sandbox_policy TEXT NOT NULL,
            approval_mode TEXT NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0,
            updated_at_ms INTEGER,
            model TEXT
        )
        """
    )
    con.execute(
        """
        INSERT INTO threads (
            id, rollout_path, created_at, updated_at, source, model_provider,
            cwd, title, sandbox_policy, approval_mode, archived, updated_at_ms, model
        ) VALUES (?, ?, 1, 1, 'vscode', 'openai', ?, ?, '', '', 0, ?, ?)
        """,
        (sid, str(rollout), str(cwd), title, 1778227200000, model),
    )
    con.commit()
    con.close()


def test_read_codex_rollout_session_extracts_and_redacts(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    sid = "019e0000-0000-7000-9000-000000000000"
    rollout = tmp_path / "rollout.jsonl"
    _write_rollout(rollout, sid=sid, cwd=bundle)

    session = read_codex_rollout_session(rollout, verbose=True)

    assert session is not None
    assert session.id == sid
    assert session.source == "codex"
    assert session.cwd == str(bundle)
    assert session.model == "gpt-5.5"
    assert [t.role for t in session.turns] == ["user", "assistant"]
    assert "secret-token" not in session.turns[0].text
    assert "Authorization: Bearer [REDACTED]" in session.turns[0].text
    assert session.turns[1].text == "Done. I will write the prompts file."


def test_codex_rollout_keeps_only_canonical_human_user_message(
    tmp_path: Path,
) -> None:
    """Modern rollouts mirror prompts into response_item rows and inject
    instruction envelopes before turn_context.  Neither is a second user turn.
    """
    rollout = tmp_path / "rollout.jsonl"
    rows = [
        {
            "timestamp": "2026-08-15T01:00:00Z",
            "type": "session_meta",
            "payload": {"id": "human-session", "cwd": str(tmp_path)},
        },
        {
            "timestamp": "2026-08-15T01:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "<INSTRUCTIONS>internal</INSTRUCTIONS>"}],
            },
        },
        {
            "timestamp": "2026-08-15T01:00:01Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol"},
        },
        {
            "timestamp": "2026-08-15T01:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Fix the watcher."}],
            },
        },
        {
            "timestamp": "2026-08-15T01:00:02Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Fix the watcher."},
        },
        {
            "timestamp": "2026-08-15T01:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Fixed."}],
            },
        },
    ]
    rollout.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    session = read_codex_rollout_session(rollout, verbose=True)

    assert session is not None
    assert session.model == "gpt-5.6-sol"
    assert [turn.role for turn in session.turns] == ["user", "assistant"]
    assert session.turns[0].text == "Fix the watcher."
    assert all("INSTRUCTIONS" not in (turn.text or "") for turn in session.turns)


def test_codex_rollout_omits_internal_approval_review_session(
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "approval-review.jsonl"
    rows = [
        {
            "timestamp": "2026-08-15T01:00:00Z",
            "type": "session_meta",
            "payload": {"id": "approval-session", "cwd": str(tmp_path)},
        },
        {
            "timestamp": "2026-08-15T01:00:01Z",
            "type": "turn_context",
            "payload": {"model": "codex-auto-review"},
        },
        {
            "timestamp": "2026-08-15T01:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "The following is the Codex agent history whose request action you are assessing.\n[tool results omitted]",
            },
        },
        {
            "timestamp": "2026-08-15T01:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": '{"outcome":"allow"}'}],
            },
        },
    ]
    rollout.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    assert read_codex_rollout_session(rollout, verbose=True) is None


def test_codex_rollout_preserves_commentary_and_final_answer_phases(
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "phases.jsonl"
    rows = [
        {
            "timestamp": "2026-08-15T01:00:00Z",
            "type": "session_meta",
            "payload": {"id": "phase-session", "cwd": str(tmp_path)},
        },
        {
            "timestamp": "2026-08-15T01:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Ship it."},
        },
        {
            "timestamp": "2026-08-15T01:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "Deploying now."}],
            },
        },
        {
            "timestamp": "2026-08-15T01:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Deployed."}],
            },
        },
    ]
    rollout.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    session = read_codex_rollout_session(rollout, verbose=True)

    assert session is not None
    assert [turn.phase for turn in session.turns] == [
        None,
        "commentary",
        "final_answer",
    ]


def test_list_recent_codex_sessions_omits_internal_approval_review(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    rollout = home / "approval.jsonl"
    _write_rollout(rollout, sid="approval", cwd=bundle)
    _write_codex_state(
        home,
        sid="approval",
        title="The following is the Codex agent history [/ -]",
        cwd=bundle,
        rollout=rollout,
        model="codex-auto-review",
    )
    monkeypatch.setenv("CODEX_CLI_HOME", str(home))

    assert list_recent_codex_sessions(bundle) == []


def test_list_recent_codex_sessions_from_state_db(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    sid = "019e1111-1111-7111-9111-111111111111"
    rollout = home / "sessions" / "2026" / "05" / "08" / "rollout.jsonl"
    _write_rollout(rollout, sid=sid, cwd=bundle)
    _write_codex_state(home, sid=sid, title="Capture current chat", cwd=bundle, rollout=rollout)
    monkeypatch.setenv("CODEX_CLI_HOME", str(home))

    recent = list_recent_codex_sessions(bundle)

    assert len(recent) == 1
    assert recent[0].id == sid
    assert recent[0].title == "Capture current chat"
    assert recent[0].turn_count == 2


def test_read_codex_sessions_includes_codex_desktop_rollouts(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    sid = "019e1333-1111-7111-9111-111111111111"
    rollout = home / "sessions" / "2026" / "05" / "08" / "rollout.jsonl"
    _write_rollout(rollout, sid=sid, cwd=bundle)
    _write_codex_state(home, sid=sid, title="Desktop auto capture", cwd=bundle, rollout=rollout)
    monkeypatch.setenv("CODEX_CLI_HOME", str(home))

    sessions = list(read_codex_sessions(bundle, verbose=True))

    assert len(sessions) == 1
    assert sessions[0].id == sid
    assert sessions[0].source == "codex"
    assert sessions[0].title == "Desktop auto capture"


def test_redact_text_common_secret_patterns() -> None:
    text = "api_key=abc123 Authorization: Bearer token123 sk-abcdefghijklmnopqrstuvwxyz"
    redacted = redact_text(text)
    assert "abc123" not in redacted
    assert "token123" not in redacted
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in redacted


def test_codex_capture_command_imports_selected_recent_chat(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "spec.yaml").write_text(
        'schema: "spec/v0.1"\nname: Test\nspec:\n  entry: docs/product.md\n',
        encoding="utf-8",
    )
    sid = "019e2222-2222-7222-9222-222222222222"
    rollout = home / "sessions" / "2026" / "05" / "08" / "rollout.jsonl"
    _write_rollout(rollout, sid=sid, cwd=bundle)
    _write_codex_state(home, sid=sid, title="Codex capture command", cwd=bundle, rollout=rollout)
    monkeypatch.setenv("CODEX_CLI_HOME", str(home))
    monkeypatch.chdir(bundle)

    result = CliRunner().invoke(
        cli,
        ["codex", "capture", "--index", "1"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    dest = bundle / "prompts" / "detached.prompts"
    pf = read_prompts_file(dest)
    assert len(pf.sessions) == 1
    assert pf.sessions[0].id == sid
    assert pf.sessions[0].source == "codex"
    assert pf.sessions[0].title == "Codex capture command"


def test_prompts_capture_source_codex_reads_desktop_rollouts(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "spec.yaml").write_text(
        'schema: "spec/v0.1"\nname: Test\nspec:\n  entry: docs/product.md\n',
        encoding="utf-8",
    )
    sid = "019e3333-3333-7333-9333-333333333333"
    rollout = home / "sessions" / "2026" / "05" / "08" / "rollout.jsonl"
    _write_rollout(rollout, sid=sid, cwd=bundle)
    _write_codex_state(home, sid=sid, title="All-source capture", cwd=bundle, rollout=rollout)
    monkeypatch.setenv("CODEX_CLI_HOME", str(home))
    monkeypatch.chdir(bundle)

    result = CliRunner().invoke(
        cli,
        ["prompts", "capture", "--source", "codex"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    dest = bundle / "prompts" / "detached.prompts"
    pf = read_prompts_file(dest)
    assert len(pf.sessions) == 1
    assert pf.sessions[0].id == sid
    assert pf.sessions[0].source == "codex"
