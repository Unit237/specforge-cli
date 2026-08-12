"""Session ordering for ``spec watch`` producer."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from spec_cli.prompts.schema import Session, Turn
from spec_cli.realtime import watcher as watcher_mod
from spec_cli.preferences import Preferences


def test_iter_local_sessions_newest_first(monkeypatch, tmp_path: Path) -> None:
    t_old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    t_new = datetime(2025, 6, 1, tzinfo=timezone.utc)

    def _fake_cursor(_paths, **kwargs):  # type: ignore[no-untyped-def]
        yield Session(
            id="cursor-old",
            source="cursor",
            turns=[Turn(role="user", text="a", at=t_old)],
            started_at=t_old,
            ended_at=t_old,
        )
        yield Session(
            id="cursor-new",
            source="cursor",
            turns=[Turn(role="user", text="b", at=t_new)],
            started_at=t_new,
            ended_at=t_new,
        )

    monkeypatch.setattr(
        watcher_mod,
        "claude_code_store_root",
        lambda: Path("/__no_such_claude_store__"),
    )
    monkeypatch.setattr(
        watcher_mod,
        "cursor_workspace_storage_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(watcher_mod, "read_cursor_sessions", _fake_cursor)
    monkeypatch.setattr(watcher_mod, "codex_transcript_store_available", lambda: False)

    ids = [s.id for s in watcher_mod._iter_local_sessions([tmp_path])]
    assert ids == ["cursor-new", "cursor-old"]


def test_ambiguous_parent_workspace_sessions_require_touched_bundle(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    signal = workspace / "signal"
    actionairy = workspace / "actionairy"
    signal.mkdir(parents=True)
    actionairy.mkdir()
    prefs = Preferences(bundles=[str(signal), str(actionairy)])
    monkeypatch.setattr(watcher_mod, "load_preferences", lambda: prefs)

    ambiguous = Session(
        id="ambiguous",
        source="codex",
        cwd=str(workspace),
        turns=[Turn(role="user", text="work across the workspace")],
    )
    actionairy_only = Session(
        id="actionairy-only",
        source="codex",
        cwd=str(workspace),
        paths_touched=["actionairy/lib/contact.dart"],
        turns=[Turn(role="user", text="fix a contact")],
    )
    signal_only = Session(
        id="signal-only",
        source="codex",
        cwd=str(workspace),
        paths_touched=["signal/src/reply.ts"],
        turns=[Turn(role="user", text="fix a reply")],
    )

    scoped = list(
        watcher_mod._scoped_sessions(
            [ambiguous, actionairy_only, signal_only], [signal]
        )
    )

    assert [session.id for session in scoped] == ["signal-only"]


def test_parent_workspace_session_is_allowed_for_only_registered_child(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    signal = workspace / "signal"
    signal.mkdir(parents=True)
    prefs = Preferences(bundles=[str(signal)])
    monkeypatch.setattr(watcher_mod, "load_preferences", lambda: prefs)
    session = Session(
        id="only-child",
        source="claude_code",
        cwd=str(workspace),
        turns=[Turn(role="user", text="fix the project")],
    )

    assert list(watcher_mod._scoped_sessions([session], [signal])) == [session]


def test_live_baseline_skips_existing_transcript_history(monkeypatch, tmp_path):
    session = Session(
        id="old-session",
        source="codex",
        turns=[
            Turn(role="user", text="old prompt"),
            Turn(role="assistant", text="old answer"),
        ],
    )
    cursor = watcher_mod.LiveCursor.load(tmp_path, project_id=7)
    monkeypatch.setattr(
        watcher_mod, "_iter_local_sessions", lambda _paths: iter([session])
    )

    count = watcher_mod._establish_live_baseline(cursor, tmp_path)

    assert count == 1
    assert cursor.turns_broadcast_for(session.id) == 2
    assert cursor.producer_baseline_version == 1
    assert watcher_mod._establish_live_baseline(cursor, tmp_path) == 0
