"""Session ordering for ``spec watch`` producer."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import threading

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


def test_ambiguous_parent_session_is_workspace_routed_once(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    actionairy = workspace / "actionairy"
    signal = workspace / "signal"
    actionairy.mkdir(parents=True)
    signal.mkdir()
    prefs = Preferences(bundles=[str(signal), str(actionairy)])
    monkeypatch.setattr(watcher_mod, "load_preferences", lambda: prefs)
    session = Session(
        id="workspace-only",
        source="codex",
        cwd=str(workspace),
        turns=[Turn(role="user", text="plan work across products")],
    )

    assert watcher_mod._session_route(session, [actionairy]) == "workspace"
    assert watcher_mod._session_route(session, [signal]) == "skip"
    assert list(watcher_mod._scoped_sessions([session], [actionairy])) == [session]
    assert list(watcher_mod._scoped_sessions([session], [signal])) == []


def test_parent_session_touching_multiple_repos_is_workspace_routed_once(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    actionairy = workspace / "actionairy"
    signal = workspace / "signal"
    actionairy.mkdir(parents=True)
    signal.mkdir()
    prefs = Preferences(bundles=[str(signal), str(actionairy)])
    monkeypatch.setattr(watcher_mod, "load_preferences", lambda: prefs)
    session = Session(
        id="cross-repo",
        source="codex",
        cwd=str(workspace),
        paths_touched=["actionairy/app.py", "signal/index.ts"],
        turns=[Turn(role="user", text="coordinate both products")],
    )

    assert watcher_mod._session_route(session, [actionairy]) == "workspace"
    assert watcher_mod._session_route(session, [signal]) == "skip"


def test_workspace_session_posts_only_to_workspace_endpoint(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    actionairy = workspace / "actionairy"
    signal = workspace / "signal"
    actionairy.mkdir(parents=True)
    signal.mkdir()
    prefs = Preferences(bundles=[str(signal), str(actionairy)])
    monkeypatch.setattr(watcher_mod, "load_preferences", lambda: prefs)
    session = Session(
        id="workspace-post",
        source="codex",
        cwd=str(workspace),
        turns=[Turn(role="user", text="plan without selecting a repo")],
    )
    monkeypatch.setattr(
        watcher_mod, "_iter_local_sessions", lambda _paths: iter([session])
    )
    monkeypatch.setattr(
        watcher_mod, "historical_bundle_paths", lambda _root: [actionairy]
    )

    class _Git:
        branch = "main"
        commit_sha = "abc123"

    class _Poster:
        def __init__(self) -> None:
            self.events = []

        def send(self, event, *, timeout=None):  # type: ignore[no-untyped-def]
            self.events.append(event)
            return True, len(self.events)

    monkeypatch.setattr(watcher_mod, "read_git_context", lambda _root: _Git())
    project_poster = _Poster()
    workspace_poster = _Poster()
    cursor = watcher_mod.LiveCursor.load(actionairy, project_id=7)
    opts = watcher_mod.WatcherOptions(
        project_id=7,
        project_label="alice/actionairy",
        api_base="https://example.test",
        access_token="token",
        self_user_id=None,
    )

    posted = watcher_mod._producer_tick(
        bundle_root=actionairy,
        cursor=cursor,
        poster=project_poster,
        workspace_poster=workspace_poster,
        opts=opts,
        stop_event=threading.Event(),
    )

    assert posted == 1
    assert project_poster.events == []
    assert len(workspace_poster.events) == 1
    assert workspace_poster.events[0].branch is None
    assert workspace_poster.events[0].commit_sha is None


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

    # Turns written while Spec was stopped are still pre-join history when a
    # new watcher starts. Re-baseline the exact current horizon every time.
    session.turns.extend(
        [
            Turn(role="user", text="prompt while stopped"),
            Turn(role="assistant", text="answer while stopped"),
        ]
    )
    assert watcher_mod._establish_live_baseline(cursor, tmp_path) == 1
    assert cursor.turns_broadcast_for(session.id) == 4

    # Parser compaction can also shrink a transcript between starts; the join
    # horizon follows the source instead of replaying from the old index.
    session.turns[:] = session.turns[:1]
    assert watcher_mod._establish_live_baseline(cursor, tmp_path) == 1
    assert cursor.turns_broadcast_for(session.id) == 1
