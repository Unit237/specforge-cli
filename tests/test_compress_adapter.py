from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from spec_cli.cli import cli
from spec_cli.prompts import read_prompts_file
from spec_cli.sources.compress import read_compress_sessions


def _write_session(store: Path, *, cwd: Path, session_id: str = "compress-1") -> None:
    store.mkdir(parents=True, exist_ok=True)
    (store / f"{session_id}.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "cwd": str(cwd),
                "model": "openai/test",
                "status": "running",
                "started_at": "2026-08-10T01:00:00+00:00",
                "updated_at": "2026-08-10T01:00:01+00:00",
                "messages": [
                    {"role": "system", "content": "private system prompt"},
                    {"role": "user", "content": "Fix auth"},
                    {
                        "role": "assistant",
                        "content": "Updating validation.",
                        "tool_calls": [
                            {
                                "id": "one",
                                "type": "function",
                                "function": {
                                    "name": "StrReplace",
                                    "arguments": json.dumps(
                                        {
                                            "path": "src/auth.py",
                                            "old_string": "old",
                                            "new_string": "new",
                                        }
                                    ),
                                },
                            }
                        ],
                    },
                    {"role": "tool", "content": "file contents are never captured"},
                ],
            }
        ),
        encoding="utf-8",
    )


def test_reads_workspace_scoped_compress_session(tmp_path, monkeypatch):
    bundle = tmp_path / "repo"
    bundle.mkdir()
    store = tmp_path / "sessions"
    _write_session(store, cwd=bundle)
    monkeypatch.setenv("COMPRESS_SESSION_DIR", str(store))

    sessions = list(read_compress_sessions(bundle, verbose=True))

    assert len(sessions) == 1
    session = sessions[0]
    assert session.source == "compress"
    assert session.model == "openai/test"
    assert [turn.role for turn in session.turns] == ["user", "assistant"]
    assert session.turns[1].tool_calls[0].name == "StrReplace"
    assert session.paths_touched == ["src/auth.py"]
    assert "file contents" not in repr(session)


def test_ignores_sessions_from_another_workspace_and_malformed_files(tmp_path, monkeypatch):
    bundle = tmp_path / "repo"
    bundle.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    store = tmp_path / "sessions"
    _write_session(store, cwd=other)
    (store / "broken.json").write_text("{", encoding="utf-8")
    (store / "old-shape.json").write_text(
        json.dumps({"session_id": ["not-a-string"], "messages": "old"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("COMPRESS_SESSION_DIR", str(store))

    assert list(read_compress_sessions(bundle)) == []


def test_machine_wide_read_includes_another_workspace(tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    store = tmp_path / "sessions"
    _write_session(store, cwd=other)
    monkeypatch.setenv("COMPRESS_SESSION_DIR", str(store))

    sessions = list(read_compress_sessions(None, verbose=True))

    assert [session.id for session in sessions] == ["compress-1"]
    assert sessions[0].cwd == str(other.resolve())


@pytest.mark.parametrize("source", ["compress", "all"])
def test_prompts_capture_includes_compress(tmp_path, monkeypatch, source):
    bundle = tmp_path / "repo"
    bundle.mkdir()
    (bundle / "spec.yaml").write_text(
        'schema: "spec/v0.1"\nname: Test\nspec:\n  entry: docs/product.md\n',
        encoding="utf-8",
    )
    store = tmp_path / "sessions"
    _write_session(store, cwd=bundle)
    monkeypatch.setenv("COMPRESS_SESSION_DIR", str(store))
    monkeypatch.chdir(bundle)

    result = CliRunner().invoke(
        cli,
        ["prompts", "capture", "--source", source],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    captured = read_prompts_file(bundle / "prompts" / "detached.prompts")
    assert [(session.id, session.source) for session in captured.sessions] == [
        ("compress-1", "compress")
    ]
