"""Unit tests for ``spec_cli.realtime.tracker.LiveCursor``.

The tracker is the durable cursor that prevents ``spec watch`` from
re-broadcasting turns it has already posted, and lets the SSE consumer
resume from where the previous run left off via ``Last-Event-ID``.
Mistakes here would either flood the team feed with duplicates (bad)
or skip events on resume (worse), so the contract is pinned tightly.
"""
from __future__ import annotations

import json

from spec_cli.realtime.tracker import (
    CURSOR_DIRNAME,
    CURSOR_FILENAME,
    SCHEMA_VERSION,
    PRODUCER_BASELINE_VERSION,
    LiveCursor,
)


def test_load_returns_fresh_cursor_when_file_missing(tmp_path):
    cursor = LiveCursor.load(tmp_path, project_id=42)
    assert cursor.project_id == 42
    assert cursor.last_received_id is None
    assert cursor.broadcast_turns == {}


def test_record_broadcast_only_moves_forward(tmp_path):
    cursor = LiveCursor.load(tmp_path, project_id=1)
    cursor.record_broadcast("session-A", 3)
    cursor.record_broadcast("session-A", 5)
    assert cursor.turns_broadcast_for("session-A") == 5
    cursor.record_broadcast("session-A", 4)
    assert cursor.turns_broadcast_for("session-A") == 5


def test_record_received_only_moves_forward(tmp_path):
    cursor = LiveCursor.load(tmp_path, project_id=1)
    cursor.record_received(10)
    cursor.record_received(7)
    cursor.record_received(15)
    assert cursor.last_received_id == 15


def test_save_then_load_round_trips(tmp_path):
    cursor = LiveCursor.load(tmp_path, project_id=99)
    cursor.record_broadcast("abc", 7)
    cursor.record_broadcast("xyz", 12)
    cursor.record_received(101)
    cursor.mark_producer_baseline()
    cursor.save()

    reloaded = LiveCursor.load(tmp_path, project_id=99)
    assert reloaded.project_id == 99
    assert reloaded.last_received_id == 101
    assert reloaded.turns_broadcast_for("abc") == 7
    assert reloaded.turns_broadcast_for("xyz") == 12
    assert reloaded.producer_baseline_version == PRODUCER_BASELINE_VERSION


def test_save_writes_under_dot_spec(tmp_path):
    cursor = LiveCursor.load(tmp_path, project_id=2)
    cursor.record_broadcast("s", 1)
    cursor.save()
    expected = tmp_path / CURSOR_DIRNAME / CURSOR_FILENAME
    assert expected.is_file()
    payload = json.loads(expected.read_text())
    assert payload["schema"] == SCHEMA_VERSION
    assert payload["project_id"] == 2


def test_project_change_resets_cursor(tmp_path):
    """Bundle was retargeted — receiving the old project's events on
    the new one would be wrong, so the loader resets."""
    cursor = LiveCursor.load(tmp_path, project_id=1)
    cursor.record_broadcast("session-A", 5)
    cursor.record_received(99)
    cursor.save()

    # Same path, different project id.
    new_cursor = LiveCursor.load(tmp_path, project_id=2)
    assert new_cursor.last_received_id is None
    assert new_cursor.broadcast_turns == {}


def test_malformed_file_does_not_raise(tmp_path):
    (tmp_path / CURSOR_DIRNAME).mkdir()
    (tmp_path / CURSOR_DIRNAME / CURSOR_FILENAME).write_text(
        "not valid json {{{", encoding="utf-8"
    )
    cursor = LiveCursor.load(tmp_path, project_id=1)
    assert cursor.last_received_id is None
    # And we can still save over the malformed file.
    cursor.record_broadcast("s", 1)
    cursor.save()
    reloaded = LiveCursor.load(tmp_path, project_id=1)
    assert reloaded.turns_broadcast_for("s") == 1


def test_turn_post_key_includes_content_fingerprint():
    """Index-only keys from older builds must not match remapped turns."""
    from datetime import datetime, timezone

    at = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
    k1 = LiveCursor.turn_post_key(2, "user", at, text="hello")
    k2 = LiveCursor.turn_post_key(2, "user", at, text="different prompt")
    assert k1 != k2
    assert len(k1.rsplit(":", 1)[-1]) == 16


def test_prune_posted_keys_drops_inflated_indices(tmp_path):
    cursor = LiveCursor.load(tmp_path, project_id=1)
    sid = "sess"
    with cursor._lock:
        cursor.posted_turn_keys[sid] = {"20:user:abc", "21:assistant:def", "5:user:ghi"}
    cursor.prune_posted_keys_from_index(sid, 20)
    assert cursor.posted_turn_keys[sid] == {"5:user:ghi"}


def test_is_turn_posted_ignores_legacy_index_only_keys(tmp_path):
    from spec_cli.prompts.schema import Turn

    cursor = LiveCursor.load(tmp_path, project_id=1)
    sid = "sess"
    turn = Turn(role="user", text="real prompt", at=None)
    with cursor._lock:
        cursor.posted_turn_keys[sid] = {"2:user"}
    assert not cursor.is_turn_posted(sid, 2, turn)


def test_save_is_atomic_via_rename(tmp_path):
    """The cursor file must be replaced atomically so a kill in
    flight can't leave half-written JSON behind. This test asserts the
    *outcome* — a saved file always parses — without depending on a
    specific syscall sequence.
    """
    cursor = LiveCursor.load(tmp_path, project_id=1)
    for i in range(50):
        cursor.record_broadcast(f"s-{i}", i + 1)
        cursor.save()
        # File must always parse — no half-written intermediate state.
        path = tmp_path / CURSOR_DIRNAME / CURSOR_FILENAME
        json.loads(path.read_text())
