from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

from spec_cli.realtime.coordination import (
    CoordinationCache,
    TeamCoordinationMirror,
    event_targets_bundle,
)
from spec_cli.realtime.events import IncomingEvent, ToolCallPayload


NOW = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)


def _event(
    event_id: int,
    *,
    role: str,
    session: str,
    source: str = "codex",
    user_id: int = 1,
    handle: str = "alice",
    text: str | None = None,
    summary: str | None = None,
    paths: list[str] | None = None,
    tools: list[ToolCallPayload] | None = None,
    phase: str | None = None,
    closes_event_id: int | None = None,
    seconds: int = 0,
) -> IncomingEvent:
    at = NOW + timedelta(seconds=seconds)
    return IncomingEvent(
        id=event_id,
        project_id=10,
        session_id=session,
        source=source,
        role=role,
        branch="main",
        commit_sha="abc",
        model="test-model",
        phase=phase,
        summary=summary,
        text=text,
        title="Session title",
        cwd=None,
        paths_touched=paths or [],
        turn_at=at,
        received_at=at,
        author_user_id=user_id,
        author_handle=handle,
        author_name=handle.title(),
        author_avatar_url=None,
        tool_calls=tools or [],
        closes_event_id=closes_event_id,
        broadcast_client_id=f"client-{user_id}",
    )


def test_round_lifecycle_keeps_handoff_until_last_agent_finishes(tmp_path):
    cache = CoordinationCache(tmp_path)
    assert cache.apply_event(_event(1, role="user", session="a", text="Build auth"))
    assert cache.apply_event(
        _event(
            2,
            role="assistant",
            session="a",
            summary="Editing token validation",
            paths=["src/auth.py"],
            tools=[ToolCallPayload("Edit", {"file_path": "src/auth.py"})],
            seconds=1,
        )
    )
    assert cache.apply_event(
        _event(
            3,
            role="user",
            session="b",
            source="compress",
            user_id=2,
            handle="bob",
            text="Add auth tests",
            seconds=2,
        )
    )
    assert cache.apply_event(_event(4, role="assistant_closed", session="a", seconds=3))

    snapshot = cache.snapshot(now=NOW + timedelta(seconds=4))
    assert snapshot is not None
    assert [row["session_id"] for row in snapshot["active"]] == ["b"]
    assert snapshot["recent_outcomes"][0]["session_id"] == "a"
    assert snapshot["recent_outcomes"][0]["outcome"] == "Editing token validation"
    assert snapshot["files_index"] == {}

    assert cache.apply_event(
        _event(
            5,
            role="assistant_closed",
            session="b",
            source="compress",
            user_id=2,
            handle="bob",
            seconds=5,
        )
    )
    assert cache.snapshot(now=NOW + timedelta(seconds=6)) is None


def test_same_session_new_user_starts_new_generation(tmp_path):
    cache = CoordinationCache(tmp_path)
    cache.apply_event(_event(1, role="user", session="a", text="First"))
    cache.apply_event(_event(2, role="user", session="a", text="Second", seconds=1))
    snapshot = cache.snapshot(now=NOW + timedelta(seconds=2))
    assert snapshot is not None
    assert snapshot["active"][0]["generation"] == 2
    assert snapshot["active"][0]["objective"] == "Second"
    assert snapshot["recent_outcomes"][0]["generation"] == 1


def test_duplicate_and_out_of_order_events_do_not_rewind(tmp_path):
    cache = CoordinationCache(tmp_path)
    cache.apply_event(_event(2, role="user", session="a", text="New"))
    assert cache.apply_event(_event(1, role="user", session="a", text="Old")) is False
    snapshot = cache.snapshot(now=NOW + timedelta(seconds=1))
    assert snapshot is not None
    assert snapshot["active"][0]["objective"] == "New"


def test_workspace_event_requires_exact_bundle_telemetry(tmp_path):
    bundle = tmp_path / "repo"
    sibling = tmp_path / "sibling"
    bundle.mkdir()
    sibling.mkdir()

    inside = _event(
        1,
        role="user",
        session="inside",
        paths=["repo/src/auth.py"],
    )
    inside.project_id = 0
    inside.cwd = str(tmp_path)
    outside = _event(
        2,
        role="user",
        session="outside",
        paths=["sibling/src/auth.py"],
    )
    outside.project_id = 0
    outside.cwd = str(tmp_path)
    ambiguous = _event(
        3,
        role="user",
        session="ambiguous",
        paths=["src/auth.py"],
    )
    ambiguous.project_id = 0
    ambiguous.cwd = None

    assert event_targets_bundle(inside, bundle)
    assert not event_targets_bundle(outside, bundle)
    assert not event_targets_bundle(ambiguous, bundle)

    cache = CoordinationCache(bundle)
    assert cache.accepts_event(inside, project_id=10)
    assert not cache.accepts_event(outside, project_id=10)


def test_active_workspace_round_accepts_pathless_close(tmp_path):
    cache = CoordinationCache(tmp_path)
    user = _event(1, role="user", session="a", text="Build auth")
    user.project_id = 0
    user.cwd = str(tmp_path)
    close = _event(2, role="assistant_closed", session="a", seconds=1)
    close.project_id = 0
    close.cwd = None
    close.paths_touched = []

    assert cache.apply_event(user)
    assert cache.tracks_event_round(close)


def test_delayed_close_from_prior_generation_does_not_close_new_prompt(tmp_path):
    cache = CoordinationCache(tmp_path)
    cache.apply_event(_event(1, role="user", session="a", text="First"))
    cache.apply_event(_event(2, role="assistant", session="a", summary="First done"))
    cache.apply_event(_event(3, role="user", session="a", text="Second"))

    assert (
        cache.apply_event(
            _event(
                4,
                role="assistant_closed",
                session="a",
                closes_event_id=2,
            )
        )
        is False
    )
    snapshot = cache.snapshot(now=NOW + timedelta(seconds=5))
    assert snapshot is not None
    assert snapshot["active"][0]["objective"] == "Second"


def test_commentary_close_keeps_agent_round_active_until_final_answer(tmp_path):
    """Codex closes each assistant bubble, including progress commentary.

    A commentary bubble ending is not the task ending: the round and its path
    claim must remain visible until Codex emits a final-answer bubble.
    """
    cache = CoordinationCache(tmp_path)
    cache.apply_event(_event(1, role="user", session="a", text="Build auth"))
    cache.apply_event(
        _event(
            2,
            role="assistant",
            session="a",
            summary="Editing token validation",
            paths=["src/auth.py"],
            phase="commentary",
            seconds=1,
        )
    )

    assert cache.apply_event(
        _event(
            3,
            role="assistant_closed",
            session="a",
            closes_event_id=2,
            seconds=2,
        )
    )
    snapshot = cache.snapshot(now=NOW + timedelta(seconds=3))
    assert snapshot is not None
    assert snapshot["active"][0]["phase"] == "commentary"
    claim = snapshot["files_index"]["src/auth.py"][0]
    assert claim["kind"] == "task_claim"
    assert claim["session_id"] == "a"

    cache.apply_event(
        _event(
            4,
            role="assistant",
            session="a",
            summary="Auth complete",
            phase="final_answer",
            seconds=4,
        )
    )
    cache.apply_event(
        _event(
            5,
            role="assistant_closed",
            session="a",
            closes_event_id=4,
            seconds=5,
        )
    )
    assert cache.snapshot(now=NOW + timedelta(seconds=6)) is None


def test_stale_round_expires_and_mirror_deletes_files(tmp_path):
    cache = CoordinationCache(tmp_path, freshness_secs=10)
    mirror = TeamCoordinationMirror(tmp_path)
    cache.apply_event(
        _event(
            1,
            role="user",
            session="a",
            text="Build auth",
            paths=["src/auth.py"],
        )
    )
    assert mirror.sync(cache, now=NOW + timedelta(seconds=1))
    assert mirror.json_path.is_file()
    assert mirror.md_path.is_file()
    body = json.loads(mirror.json_path.read_text(encoding="utf-8"))
    assert body["active"][0]["objective"] == "Build auth"
    assert "Read this before planning or editing" in mirror.md_path.read_text(encoding="utf-8")
    markdown = mirror.md_path.read_text(encoding="utf-8")
    assert markdown.index("## Claimed path index") < markdown.index(
        "## Active agent rounds"
    )

    assert mirror.sync(cache, now=NOW + timedelta(seconds=11))
    assert not mirror.json_path.exists()
    assert not mirror.md_path.exists()


def test_fresh_coordination_snapshot_survives_watcher_restart(tmp_path):
    original = CoordinationCache(tmp_path, freshness_secs=120)
    original.apply_event(_event(10, role="user", session="a", text="Build auth"))
    original.apply_event(
        _event(
            11,
            role="assistant",
            session="a",
            summary="Editing token validation",
            paths=["src/auth.py"],
            seconds=1,
        )
    )
    snapshot = original.snapshot(now=NOW + timedelta(seconds=2))

    restarted = CoordinationCache(tmp_path, freshness_secs=120)
    assert restarted.restore_snapshot(snapshot, now=NOW + timedelta(seconds=30))
    restored = restarted.snapshot(now=NOW + timedelta(seconds=31))

    assert restored is not None
    assert restored["active"][0]["session_id"] == "a"
    assert list(restored["files_index"]) == ["src/auth.py"]
    # The restored event cursor prevents a replayed older Cloud frame from
    # rewinding or closing this still-active generation.
    assert not restarted.apply_event(
        _event(9, role="assistant_closed", session="a", seconds=20)
    )


def test_tool_paths_are_normalized_and_indexed(tmp_path):
    cache = CoordinationCache(tmp_path)
    cache.apply_event(_event(1, role="user", session="a", text="Edit files"))
    cache.apply_event(
        _event(
            2,
            role="assistant",
            session="a",
            tools=[
                ToolCallPayload("Edit", {"file_path": "./src/auth.py"}),
                ToolCallPayload("Write", {"path": "../outside.py"}),
            ],
            seconds=1,
        )
    )
    snapshot = cache.snapshot(now=NOW + timedelta(seconds=2))
    assert snapshot is not None
    assert list(snapshot["files_index"]) == ["src/auth.py"]


def test_concurrent_snapshots_remain_valid_during_updates(tmp_path):
    cache = CoordinationCache(tmp_path)
    cache.apply_event(_event(1, role="user", session="a", text="Build auth"))
    snapshots: list[dict] = []
    failures: list[Exception] = []

    def _read() -> None:
        try:
            for _ in range(100):
                snapshot = cache.snapshot(now=NOW + timedelta(seconds=20))
                if snapshot is not None:
                    json.dumps(snapshot)
                    snapshots.append(snapshot)
        except Exception as exc:  # pragma: no cover - assertion captures it
            failures.append(exc)

    reader = threading.Thread(target=_read)
    reader.start()
    for event_id in range(2, 22):
        cache.apply_event(
            _event(
                event_id,
                role="assistant",
                session="a",
                summary=f"Progress {event_id}",
                seconds=event_id,
            )
        )
    reader.join(timeout=2)

    assert not failures
    assert snapshots
    assert all(snapshot["schema"] == "spec.team-coordination/v1" for snapshot in snapshots)


def test_atomic_write_failure_preserves_existing_file(tmp_path, monkeypatch):
    cache = CoordinationCache(tmp_path)
    mirror = TeamCoordinationMirror(tmp_path)
    cache.apply_event(_event(1, role="user", session="a", text="Build auth"))
    mirror.spec_dir.mkdir(parents=True)
    mirror.json_path.write_text("prior\n", encoding="utf-8")

    def _fail_replace(_source, _target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("spec_cli.realtime.coordination.os.replace", _fail_replace)

    assert mirror.sync(cache, now=NOW + timedelta(seconds=1)) is False
    assert mirror.json_path.read_text(encoding="utf-8") == "prior\n"
    assert list(mirror.spec_dir.glob("*.tmp")) == []
