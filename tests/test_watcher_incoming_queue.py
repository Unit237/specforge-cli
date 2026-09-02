"""``spec watch`` must not render SSE frames on the reader thread."""
from __future__ import annotations

import json
import threading
import time

from spec_cli.realtime.watcher import WatcherOptions, run_watcher


class _BlockingNotifier:
    """Simulate slow Rich output on the main thread."""

    def __init__(self) -> None:
        self.show_calls: list[int] = []
        self._release = threading.Event()

    def announce_connected(self, _label: str) -> None:
        pass

    def announce_broadcast_disabled(self) -> None:
        pass

    def show(self, event) -> None:  # type: ignore[no-untyped-def]
        self.show_calls.append(event.id)
        self._release.wait(timeout=2.0)

    def announce_fatal(self, _msg: str) -> None:
        pass


class _FastEnqueueConsumer:
    """Pushes several events immediately — would overflow a slow reader."""

    def __init__(self, events: list) -> None:
        self._events = list(events)
        self._stop = threading.Event()
        self.resume_ids: list[int | None] = []

    def set_resume_cursor(self, last_id) -> None:  # type: ignore[no-untyped-def]
        self.resume_ids.append(last_id)

    def stop(self) -> None:
        self._stop.set()


def test_watcher_drains_incoming_on_main_thread(monkeypatch, tmp_path) -> None:
    from spec_cli.realtime import events as evmod

    events = [
        evmod.IncomingEvent(
            id=i,
            project_id=1,
            session_id="s",
            source="cursor",
            role="user",
            branch="main",
            commit_sha=None,
            model=None,
            summary="hi",
            text=f"msg {i}",
            title=None,
            cwd=str(tmp_path),
            paths_touched=[],
            turn_at=evmod.datetime.now(evmod.timezone.utc),
            received_at=evmod.datetime.now(evmod.timezone.utc),
            author_user_id=2,
            author_handle="bob",
            author_name="Bob",
            author_avatar_url=None,
        )
        for i in (1, 2, 3)
    ]
    events.append(
        evmod.IncomingEvent(
            id=4,
            project_id=2,
            session_id="presence-other-project",
            source="cursor",
            role="presence",
            branch="main",
            commit_sha=None,
            model=None,
            summary=None,
            text=None,
            title=None,
            cwd=str(tmp_path),
            paths_touched=[],
            turn_at=evmod.datetime.now(evmod.timezone.utc),
            received_at=evmod.datetime.now(evmod.timezone.utc),
            author_user_id=2,
            author_handle="bob",
            author_name="Bob",
            author_avatar_url=None,
        )
    )
    events.append(
        evmod.IncomingEvent(
            id=5,
            project_id=2,
            session_id="other-project-prompt",
            source="cursor",
            role="user",
            branch="main",
            commit_sha=None,
            model=None,
            summary="unrelated",
            text="unrelated project prompt",
            title=None,
            cwd=str(tmp_path),
            paths_touched=[],
            turn_at=evmod.datetime.now(evmod.timezone.utc),
            received_at=evmod.datetime.now(evmod.timezone.utc),
            author_user_id=3,
            author_handle="charlie",
            author_name="Charlie",
            author_avatar_url=None,
        )
    )
    # The first frame is this install's own SSE echo. Solo users must see it
    # in the workspace feed as well as in the coordination projection.
    events[0].author_user_id = 1
    events[0].author_handle = "alice"
    events[0].author_name = "Alice"
    events[0].broadcast_client_id = "local-install"
    consumer = _FastEnqueueConsumer(events)
    notifier = _BlockingNotifier()
    consumer_calls: list[tuple[tuple, dict]] = []
    presence_apply_calls: list[int] = []

    monkeypatch.setattr(
        "spec_cli.realtime.watcher.Notifier",
        lambda *a, **kw: notifier,
    )
    monkeypatch.setattr(
        "spec_cli.realtime.watcher.HTTPPoster",
        lambda *a, **kw: None,
    )
    def _consumer_factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        consumer_calls.append((args, kwargs))
        return consumer

    monkeypatch.setattr(
        "spec_cli.realtime.watcher.SSEConsumer",
        _consumer_factory,
    )

    def _run_consumer(c, on_event, on_fatal, **kw):  # type: ignore[no-untyped-def]
        def _worker() -> None:
            for ev in events:
                on_event(ev)
            time.sleep(0.05)
            c.stop()

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return t

    monkeypatch.setattr(
        "spec_cli.realtime.watcher.run_consumer_in_thread", _run_consumer
    )
    monkeypatch.setattr(
        "spec_cli.realtime.watcher._spec_live_startup_snapshot", lambda _r: None
    )
    monkeypatch.setattr(
        "spec_cli.realtime.watcher.read_git_context",
        lambda _r: type("G", (), {"branch": "main", "commit_sha": None})(),
    )
    monkeypatch.setattr(
        "spec_cli.realtime.watcher.historical_bundle_paths", lambda _r: []
    )
    monkeypatch.setattr(
        "spec_cli.realtime.watcher.compute_local_presence",
        lambda _r: type(
            "P", (), {"files": [], "head_commit": None, "fingerprint": ""}
        )(),
    )
    monkeypatch.setattr(
        "spec_cli.realtime.watcher.TeamPresenceMirror.write", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "spec_cli.realtime.watcher.PresenceCache.apply_event",
        lambda _self, event: presence_apply_calls.append(event.id) or False,
    )

    stop = threading.Event()

    def _stop_soon() -> None:
        time.sleep(0.2)
        notifier._release.set()
        stop.set()

    threading.Thread(target=_stop_soon, daemon=True).start()

    opts = WatcherOptions(
        project_id=1,
        project_label="alice/demo",
        api_base="http://localhost",
        access_token="t",
        self_user_id=1,
        broadcast_client_id="local-install",
        receive=True,
        broadcast=False,
        presence_enabled=False,
        poll_interval=0.2,
    )
  # (tmp_path / "spec.yaml") not required — bundle_root is tmp_path
    (tmp_path / "spec.yaml").write_text(
        "schema: spec/v0.1\nname: t\ncloud:\n  project: a/b\n",
        encoding="utf-8",
    )
    (tmp_path / ".spec").mkdir(exist_ok=True)

    run_watcher(tmp_path, opts, stop_event=stop)

    assert notifier.show_calls == [1, 2, 3, 5]
    assert consumer_calls
    assert consumer_calls[0][1]["workspace"] is True
    assert consumer_calls[0][1]["include_presence"] is True
    assert "project_id" not in consumer_calls[0][1]
    assert consumer.resume_ids == []
    assert presence_apply_calls == []
    board = json.loads(
        (tmp_path / ".spec" / "team-coordination.json").read_text(encoding="utf-8")
    )
    assert {row["author"]["handle"] for row in board["active"]} == {
        "alice",
        "bob",
    }
