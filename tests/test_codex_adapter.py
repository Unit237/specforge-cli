"""Tests for the Codex transcript adapter."""

from __future__ import annotations

import json
from pathlib import Path

from spec_cli.sources.codex import encode_bundle_path, read_codex_sessions


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
                            {"type": "text", "text": "Mapping call sites first. Then I will refactor."}
                    ]
                },
            },
        ],
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    sessions = list(read_codex_sessions(bundle))
    assert len(sessions) == 1
    s = sessions[0]
    assert s.id == sid
    assert s.source == "codex"
    assert s.cwd == str(bundle.resolve())
    assert [t.role for t in s.turns] == ["user", "assistant"]
    assert s.turns[0].text == "Refactor billing please."
    assert s.turns[1].summary is not None
    assert "mapping call sites" in s.turns[1].summary.lower()
    assert s.turns[1].text is None


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

    sessions = list(read_codex_sessions(bundle, verbose=True))
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

    sessions = list(read_codex_sessions(bundle))
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
    sessions = list(read_codex_sessions(bundle))
    assert [s.id for s in sessions] == [sid]
