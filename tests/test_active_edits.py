"""Unit tests for :mod:`spec_cli.realtime.active_edits`.

The store is the source of truth for single-user multi-agent
coordination — every higher-level surface (``spec locks check``, the
Claude pre-tool-use hook, the brief renderer) consults it. The tests
exercise the contracts those callers depend on:

* Acquire / release round-trips persist to one ``$SPEC_HOME/active-edits.json``.
* Bundle roots namespace rows, so equal paths in different repos do not clash.
* Legacy per-bundle lock files migrate once into the global registry.
* TTL expiry filters locks out of ``list`` and ``holders_for`` reads
  without requiring an explicit prune.
* Same agent + session re-acquire is a *renewal* (no conflict);
  cross-agent or cross-session acquire is a *conflict* (lock granted
  but conflict list populated).
* Releases by id and by session both work; releasing an unknown id
  is a no-op (matching the hook contract where the matching
  PostToolUse fires regardless of whether PreToolUse succeeded).
* Path normalisation handles ``./``-prefixed and back-slashed input
  so two callers asking about the same file always overlap.
* The store is robust to a missing or malformed JSON file —
  callers see "no locks" rather than an exception.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from spec_cli.realtime.active_edits import (
    ACTIVE_EDITS_FILENAME,
    ACTIVE_EDITS_SCHEMA_VERSION,
    MAX_LOCK_TTL_SECS,
    ActiveEditLock,
    ActiveEditsStore,
)


@pytest.fixture
def bundle_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Throw-away bundle with an isolated machine-wide Spec home."""
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    (tmp_path / "spec.yaml").write_text("name: demo\n", encoding="utf-8")
    return tmp_path


def _store(bundle_root: Path) -> ActiveEditsStore:
    return ActiveEditsStore(bundle_root)


def test_acquire_persists_to_disk(bundle_root: Path) -> None:
    """A successful acquire writes the lock into
    ``$SPEC_HOME/active-edits.json`` immediately — no buffering. The hook
    is a one-shot subprocess and depends on the post-acquire file
    being visible to the next subprocess invocation."""
    store = _store(bundle_root)
    lock, conflicts = store.acquire(
        ["src/auth.py"],
        agent="claude_code",
        session_id="abc",
        intent="Edit",
    )
    assert conflicts == []
    file = store.path
    assert file.is_file()
    body = json.loads(file.read_text(encoding="utf-8"))
    assert body["schema"] == ACTIVE_EDITS_SCHEMA_VERSION
    assert len(body["locks"]) == 1
    on_disk = body["locks"][0]
    assert on_disk["id"] == lock.id
    assert on_disk["paths"] == ["src/auth.py"]
    assert on_disk["agent"] == "claude_code"
    assert on_disk["session_id"] == "abc"
    assert on_disk["bundle_root"] == str(bundle_root.resolve())
    assert not (bundle_root / ".spec" / ACTIVE_EDITS_FILENAME).exists()


def test_acquire_returns_conflicts_across_agents(bundle_root: Path) -> None:
    """Different agent + same path → conflict surfaces. Both locks
    are still granted (advisory) — the caller decides whether to
    proceed via ``--block`` or warn-only."""
    store = _store(bundle_root)
    first, _ = store.acquire(["src/auth.py"], agent="claude_code", session_id="abc")
    second, conflicts = store.acquire(["src/auth.py"], agent="cursor", session_id="xyz")
    assert second.id != first.id
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c.lock.id == first.id
    assert c.overlapping_paths == ["src/auth.py"]


def test_acquire_same_agent_session_is_renewal(bundle_root: Path) -> None:
    """Same ``(agent, session_id)`` re-acquire is treated as a
    renewal: no conflict, only the new lock exists afterwards. This
    is what lets a single hook session loop through many tool calls
    without piling up overlapping locks for itself."""
    store = _store(bundle_root)
    first, _ = store.acquire(["src/auth.py"], agent="claude_code", session_id="abc")
    second, conflicts = store.acquire(
        ["src/auth.py", "src/db.py"],
        agent="claude_code",
        session_id="abc",
    )
    assert conflicts == []
    locks = store.list()
    assert len(locks) == 1
    assert locks[0].id == second.id
    assert set(locks[0].paths) == {"src/auth.py", "src/db.py"}


def test_release_by_id_round_trip(bundle_root: Path) -> None:
    store = _store(bundle_root)
    lock, _ = store.acquire(["src/auth.py"], agent="cursor")
    assert store.release(lock.id) is True
    assert store.list() == []
    # Releasing again is a no-op.
    assert store.release(lock.id) is False


def test_release_unknown_id_is_noop(bundle_root: Path) -> None:
    """Hook contract: the PostToolUse hook fires regardless of
    whether the PreToolUse hook recorded a lock. An unknown id
    must not raise — better to silently no-op than to break the
    post hook on a stale invocation."""
    store = _store(bundle_root)
    assert store.release("nope") is False


def test_release_for_session_drops_all_matches(bundle_root: Path) -> None:
    """One PostToolUse call cleans up multiple lock entries when an
    agent has somehow taken more than one in the same session
    (renewal is the common case, but a forced overlap is possible
    on race)."""
    store = _store(bundle_root)
    store.acquire(["a.py"], agent="claude_code", session_id="abc")
    store.acquire(["b.py"], agent="claude_code", session_id="xyz")
    store.acquire(["c.py"], agent="cursor", session_id="abc")
    removed = store.release_for_session(agent="claude_code", session_id="abc")
    assert removed == 1
    remaining = store.list()
    remaining_agents = sorted((lk.agent, lk.session_id) for lk in remaining)
    assert remaining_agents == [("claude_code", "xyz"), ("cursor", "abc")]


def test_expired_lock_filtered_from_list(bundle_root: Path) -> None:
    """The store treats ``expires_at <= now`` as released even
    without a physical prune. This lets a crashed agent's lock
    auto-recycle on the TTL timer."""
    store = _store(bundle_root)
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    # Use a tiny TTL so the lock expires "in the past" via the
    # acquire path itself. We then manually rewrite expires_at to
    # ensure the lock is unambiguously stale (1 µs precision can
    # otherwise leave us within the comparison window).
    lock, _ = store.acquire(["src/auth.py"], agent="cursor", ttl_secs=1.0)
    file = store.path
    body = json.loads(file.read_text(encoding="utf-8"))
    body["locks"][0]["expires_at"] = past.isoformat()
    file.write_text(json.dumps(body), encoding="utf-8")

    assert store.list() == []
    assert store.list(include_expired=True) != []
    assert store.holders_for("src/auth.py") == []


def test_prune_removes_expired(bundle_root: Path) -> None:
    """``prune`` physically deletes expired entries — used by
    ``spec locks prune`` for housekeeping. Filtering already
    happens at read time, so this is purely about keeping the
    JSON small."""
    store = _store(bundle_root)
    lock, _ = store.acquire(["src/auth.py"], agent="cursor", ttl_secs=1.0)
    file = store.path
    body = json.loads(file.read_text(encoding="utf-8"))
    body["locks"][0]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=10)
    ).isoformat()
    file.write_text(json.dumps(body), encoding="utf-8")

    removed = store.prune()
    assert removed == 1
    body2 = json.loads(file.read_text(encoding="utf-8"))
    assert body2["locks"] == []


def test_ttl_clamped_to_max(bundle_root: Path) -> None:
    """A buggy caller passing an absurd TTL gets clamped down to
    the module's hard cap. Prevents one agent from pinning a lock
    for a week and breaking everyone else's edits."""
    store = _store(bundle_root)
    lock, _ = store.acquire(["src/auth.py"], agent="cursor", ttl_secs=86400 * 7)
    expected_max = datetime.now(timezone.utc) + timedelta(seconds=MAX_LOCK_TTL_SECS + 1)
    assert lock.expires_at <= expected_max


def test_path_normalisation_overlaps_match(bundle_root: Path) -> None:
    """``./src/auth.py`` and ``src/auth.py`` are the same file from
    the user's perspective. The store must agree, otherwise a hook
    passing the cwd-relative form would never see a conflict from
    another hook passing the dot-prefixed form."""
    store = _store(bundle_root)
    store.acquire(["./src/auth.py"], agent="claude_code", session_id="a")
    _, conflicts = store.acquire(["src/auth.py"], agent="cursor", session_id="b")
    assert len(conflicts) == 1


def test_directory_claim_overlaps_a_file_inside_it(bundle_root: Path) -> None:
    """A component claim must cover later file edits beneath it."""
    store = _store(bundle_root)
    store.acquire(["src/auth"], agent="codex", session_id="a")

    _, conflicts = store.acquire(
        ["src/auth/token.py"],
        agent="cursor",
        session_id="b",
    )

    assert len(conflicts) == 1
    assert conflicts[0].overlapping_paths == ["src/auth/token.py"]


def test_holders_for_returns_only_path_matches(bundle_root: Path) -> None:
    """``holders_for`` powers ``spec locks check`` — it must filter
    by path, not return every lock in the file."""
    store = _store(bundle_root)
    store.acquire(["a.py"], agent="claude_code", session_id="a")
    store.acquire(["b.py"], agent="cursor", session_id="b")
    a = store.holders_for("a.py")
    assert len(a) == 1
    assert a[0].agent == "claude_code"
    b = store.holders_for("b.py")
    assert len(b) == 1
    assert b[0].agent == "cursor"


def test_malformed_file_is_treated_as_empty(bundle_root: Path) -> None:
    """A corrupt JSON file should not crash any consumer — the
    store logs and degrades to "no locks". This matches every
    other Spec mirror file (team-presence, live-cursor)."""
    store = _store(bundle_root)
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not json", encoding="utf-8")
    assert store.list() == []
    # Acquire still works (writes a fresh file).
    lock, _ = store.acquire(["src/auth.py"], agent="cursor")
    assert lock.id


def test_missing_file_is_treated_as_empty(bundle_root: Path) -> None:
    store = _store(bundle_root)
    assert store.list() == []
    assert store.holders_for("anything") == []


def test_global_registry_namespaces_equal_paths_by_bundle(
    bundle_root: Path,
) -> None:
    other = bundle_root.parent / "other-bundle"
    other.mkdir()
    (other / "spec.yaml").write_text("name: other\n", encoding="utf-8")
    first = _store(bundle_root)
    second = _store(other)

    first_lock, _ = first.acquire(["src/auth.py"], agent="codex", session_id="first")
    second_lock, conflicts = second.acquire(
        ["src/auth.py"], agent="claude_code", session_id="second"
    )

    assert first.path == second.path
    assert conflicts == []
    assert [lock.id for lock in first.list()] == [first_lock.id]
    assert [lock.id for lock in second.list()] == [second_lock.id]
    assert {lock.id for lock in first.list_all()} == {
        first_lock.id,
        second_lock.id,
    }


def test_legacy_bundle_file_is_imported_once(bundle_root: Path) -> None:
    legacy_dir = bundle_root / ".spec"
    legacy_dir.mkdir()
    now = datetime.now(timezone.utc)
    legacy_lock = ActiveEditLock(
        id="legacy-lock",
        paths=["legacy.py"],
        agent="cursor",
        session_id="legacy-session",
        pid=123,
        host="old-machine",
        started_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    legacy_path = legacy_dir / ACTIVE_EDITS_FILENAME
    legacy_body = {"schema": 1, "locks": [legacy_lock.to_json()]}
    legacy_path.write_text(json.dumps(legacy_body), encoding="utf-8")

    store = _store(bundle_root)
    imported = store.list()
    global_body = json.loads(store.path.read_text(encoding="utf-8"))

    assert [lock.id for lock in imported] == ["legacy-lock"]
    assert imported[0].bundle_root == str(bundle_root.resolve())
    assert global_body["schema"] == ACTIVE_EDITS_SCHEMA_VERSION
    assert global_body["migrated_bundle_roots"] == [str(bundle_root.resolve())]
    assert json.loads(legacy_path.read_text(encoding="utf-8")) == legacy_body

    # A second read does not duplicate the imported row.
    assert [lock.id for lock in store.list_all()] == ["legacy-lock"]


def test_acquire_requires_at_least_one_path(bundle_root: Path) -> None:
    store = _store(bundle_root)
    with pytest.raises(ValueError):
        store.acquire([], agent="cursor")
    with pytest.raises(ValueError):
        store.acquire(["", "  "], agent="cursor")


def test_lock_is_expired_helper() -> None:
    now = datetime.now(timezone.utc)
    fresh = ActiveEditLock(
        id="x",
        paths=["a"],
        agent="x",
        session_id=None,
        pid=1,
        host="h",
        started_at=now - timedelta(seconds=10),
        expires_at=now + timedelta(seconds=10),
    )
    stale = ActiveEditLock(
        id="y",
        paths=["a"],
        agent="x",
        session_id=None,
        pid=1,
        host="h",
        started_at=now - timedelta(seconds=100),
        expires_at=now - timedelta(seconds=1),
    )
    assert fresh.is_expired() is False
    assert stale.is_expired() is True
