"""Session ordering for ``spec watch`` producer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


def test_iter_local_sessions_keeps_complete_view_of_duplicate_identity(
    monkeypatch, tmp_path: Path
) -> None:
    def _fake_cursor(_paths, **kwargs):  # type: ignore[no-untyped-def]
        yield Session(
            id="shared",
            source="cursor",
            title="complete",
            turns=[
                Turn(role="user", text="prompt"),
                Turn(role="assistant", text="answer"),
            ],
        )
        yield Session(
            id="shared",
            source="cursor",
            title="short fork",
            turns=[Turn(role="user", text="prompt")],
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
    monkeypatch.setattr(
        watcher_mod, "codex_transcript_store_available", lambda: False
    )

    sessions = list(watcher_mod._iter_local_sessions([tmp_path]))

    assert len(sessions) == 1
    assert sessions[0].title == "complete"
    assert len(sessions[0].turns) == 2


def test_machine_wide_session_scan_passes_unscoped_source_boundary(
    monkeypatch, tmp_path: Path
) -> None:
    observed_scopes = []

    def _fake_cursor(scope, **kwargs):  # type: ignore[no-untyped-def]
        observed_scopes.append(scope)
        yield Session(
            id="outside",
            source="cursor",
            cwd=str(tmp_path / "outside"),
            turns=[Turn(role="user", text="hello from elsewhere")],
        )

    monkeypatch.setattr(
        watcher_mod,
        "claude_code_store_root",
        lambda: Path("/__no_such_claude_store__"),
    )
    monkeypatch.setattr(watcher_mod, "cursor_workspace_storage_root", lambda: tmp_path)
    monkeypatch.setattr(watcher_mod, "read_cursor_sessions", _fake_cursor)
    monkeypatch.setattr(watcher_mod, "codex_transcript_store_available", lambda: False)

    sessions = list(watcher_mod._iter_local_sessions(None))

    assert observed_scopes == [None]
    assert [session.id for session in sessions] == ["outside"]


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


def test_machine_wide_producer_routes_repo_and_non_repo_sessions_once(
    monkeypatch, tmp_path: Path
) -> None:
    bundle = tmp_path / "bundle"
    outside = tmp_path / "outside"
    bundle.mkdir()
    outside.mkdir()
    sessions = [
        Session(
            id="inside",
            source="codex",
            cwd=str(bundle),
            turns=[Turn(role="user", text="inside")],
        ),
        Session(
            id="outside",
            source="codex",
            cwd=str(outside),
            turns=[Turn(role="user", text="outside")],
        ),
    ]
    observed_scopes = []

    def _sessions(scope, **_kwargs):  # type: ignore[no-untyped-def]
        observed_scopes.append(scope)
        return iter(sessions)

    monkeypatch.setattr(watcher_mod, "_iter_local_sessions", _sessions)
    monkeypatch.setattr(
        watcher_mod, "historical_bundle_paths", lambda _root: [bundle]
    )

    class _Poster:
        def __init__(self) -> None:
            self.events = []

        def send(self, event, *, timeout=None):  # type: ignore[no-untyped-def]
            self.events.append(event)
            return True, len(self.events)

    project_poster = _Poster()
    workspace_poster = _Poster()
    cursor = watcher_mod.LiveCursor.load(bundle, project_id=7)
    opts = watcher_mod.WatcherOptions(
        project_id=7,
        project_label="alice/bundle",
        api_base="https://example.test",
        access_token="token",
        self_user_id=None,
        prompt_scope="machine",
    )

    posted = watcher_mod._producer_tick(
        bundle_root=bundle,
        cursor=cursor,
        poster=project_poster,
        workspace_poster=workspace_poster,
        opts=opts,
        stop_event=threading.Event(),
        machine_wide=True,
    )

    assert posted == 2
    assert observed_scopes == [None]
    assert project_poster.events == []
    assert [event.session_id for event in workspace_poster.events] == [
        "inside",
        "outside",
    ]
    assert all(event.branch is None for event in workspace_poster.events)


def test_resumed_old_machine_chat_broadcasts_only_turns_after_join(
    monkeypatch, tmp_path: Path
) -> None:
    joined = datetime(2026, 8, 15, 4, 0, tzinfo=timezone.utc)
    session = Session(
        id="resumed-old-chat",
        source="codex",
        cwd=str(tmp_path / "outside"),
        started_at=joined - timedelta(days=30),
        ended_at=joined + timedelta(seconds=2),
        turns=[
            Turn(role="user", text="old prompt", at=joined - timedelta(days=30)),
            Turn(role="assistant", text="old answer", at=joined - timedelta(days=30)),
            Turn(role="user", text="new prompt", at=joined + timedelta(seconds=1)),
        ],
    )
    monkeypatch.setattr(
        watcher_mod,
        "_iter_local_sessions",
        lambda _scope, **_kwargs: iter([session]),
    )

    class _Poster:
        def __init__(self) -> None:
            self.events = []

        def send(self, event, *, timeout=None):  # type: ignore[no-untyped-def]
            self.events.append(event)
            return True, len(self.events)

    project_poster = _Poster()
    workspace_poster = _Poster()
    cursor = watcher_mod.LiveCursor.load(tmp_path, project_id=7)
    cursor.mark_producer_baseline()
    opts = watcher_mod.WatcherOptions(
        project_id=7,
        project_label="alice/workspace",
        api_base="https://example.test",
        access_token="token",
        self_user_id=None,
        prompt_scope="machine",
        started_at=joined,
    )

    posted = watcher_mod._producer_tick(
        bundle_root=tmp_path,
        cursor=cursor,
        poster=project_poster,
        workspace_poster=workspace_poster,
        opts=opts,
        stop_event=threading.Event(),
        machine_wide=True,
    )

    assert posted == 1
    assert project_poster.events == []
    assert [event.text for event in workspace_poster.events] == ["new prompt"]


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
