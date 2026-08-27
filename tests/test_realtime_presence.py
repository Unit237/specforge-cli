"""Tests for the file-level edit presence pipeline.

Three concentric rings of coverage:

1. ``compute_local_presence`` against a real git worktree — exercises
   the ``git diff --numstat`` parsing, untracked-file enumeration,
   monorepo subtree filtering, and the deterministic fingerprint.
2. ``PresenceCache`` semantics — apply / expire / replace — without
   any network or git involvement.
3. ``TeamPresenceMirror`` writing the canonical ``.spec/
   team-presence.json`` file from a cache + local snapshot, with
   the inverted ``files_index`` shape every hook depends on.

Each layer matters: the bottom one is the data source, the middle is
what receivers maintain in memory, and the top is the public contract
external tools (Claude Code hook, Cursor rule) consume.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from spec_cli.realtime.events import IncomingEvent, PresenceFile, PresencePayload
from spec_cli.realtime.presence import (
    LocalPresence,
    PresenceCache,
    compute_local_presence,
)
from spec_cli.realtime.presence_mirror import (
    TEAM_PRESENCE_HEARTBEAT_SECS,
    TeamPresenceMirror,
    read_team_presence,
)
from spec_cli.realtime.team_editing_brief import TEAM_EDITING_BRIEF_FILENAME


# ── helpers ────────────────────────────────────────────────────────


def _git(args: list[str], *, cwd: Path) -> None:
    """Run a git command, asserting success. Used by the fixtures
    below to set up a worktree without depending on the CLI's own
    git wrappers — keeps the test honest about the wire format we
    parse."""
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A bare-bones git repo with one committed file. Subsequent
    edits / new files reflect in ``compute_local_presence`` exactly
    the way they would for a real user.
    """
    _git(["init", "--initial-branch=main"], cwd=tmp_path)
    _git(["config", "user.email", "test@example.com"], cwd=tmp_path)
    _git(["config", "user.name", "Test"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "README.md"], cwd=tmp_path)
    _git(["commit", "-m", "init"], cwd=tmp_path)
    return tmp_path


def _make_incoming_presence_event(
    *,
    user_id: int,
    handle: str,
    files: list[PresenceFile],
    branch: str = "main",
    received_at: datetime | None = None,
    is_clean: bool = False,
    event_id: int = 1,
    broadcast_client_id: str | None = None,
) -> IncomingEvent:
    return IncomingEvent(
        id=event_id,
        project_id=1,
        session_id=f"presence:{user_id}",
        source="git",
        role="presence",
        branch=branch,
        commit_sha="abc123",
        model=None,
        summary="presence",
        text=None,
        title=None,
        cwd=None,
        paths_touched=[f.path for f in files],
        turn_at=None,
        received_at=received_at or datetime.now(timezone.utc),
        author_user_id=user_id,
        author_handle=handle,
        author_name=handle.title(),
        author_avatar_url=None,
        presence=PresencePayload(files=files, head_commit="abc123", is_clean=is_clean),
        broadcast_client_id=broadcast_client_id,
    )


# ── compute_local_presence ─────────────────────────────────────────


def test_compute_local_presence_clean_tree(git_repo):
    p = compute_local_presence(git_repo)
    assert p.is_clean is True
    assert p.files == []
    assert p.head_commit is not None
    assert p.fingerprint  # stable non-empty hash


def test_compute_local_presence_modified_file(git_repo):
    (git_repo / "README.md").write_text("hello\nworld\n", encoding="utf-8")
    p = compute_local_presence(git_repo)
    assert p.is_clean is False
    assert len(p.files) == 1
    f = p.files[0]
    assert f.path == "README.md"
    assert f.lines_added == 1
    assert f.lines_removed == 0
    assert f.untracked is False


def test_compute_local_presence_untracked_file(git_repo):
    (git_repo / "new.py").write_text("a\nb\nc\n", encoding="utf-8")
    p = compute_local_presence(git_repo)
    paths = [f.path for f in p.files]
    assert "new.py" in paths
    f = next(f for f in p.files if f.path == "new.py")
    assert f.untracked is True
    assert f.lines_added == 3
    assert f.lines_removed == 0


def test_compute_local_presence_outside_git_returns_clean(tmp_path):
    p = compute_local_presence(tmp_path)
    assert p.is_clean is True
    assert p.files == []


def test_compute_local_presence_fingerprint_is_stable(git_repo):
    """Two snapshots over the same state must hash identically — that's
    what lets the watcher debounce broadcasts. If the fingerprint
    drifts on no-op ticks we'd spam the wire."""
    (git_repo / "README.md").write_text("hello\nworld\n", encoding="utf-8")
    a = compute_local_presence(git_repo)
    b = compute_local_presence(git_repo)
    assert a.fingerprint == b.fingerprint


def test_compute_local_presence_fingerprint_changes_on_edit(git_repo):
    a = compute_local_presence(git_repo)
    (git_repo / "README.md").write_text("hello\nworld\n", encoding="utf-8")
    b = compute_local_presence(git_repo)
    assert a.fingerprint != b.fingerprint


def test_compute_local_presence_monorepo_scopes_to_bundle(git_repo):
    """When ``bundle_root`` is a subdirectory, presence must only
    surface dirty files inside it. The whole point of bundle-relative
    paths is that two bundles in one monorepo don't pollute each
    other's team-presence views."""
    bundle_dir = git_repo / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "spec.yaml").write_text("name: x\n", encoding="utf-8")
    _git(["add", "."], cwd=git_repo)
    _git(["commit", "-m", "add bundle"], cwd=git_repo)

    # Edit one file inside the bundle, one outside.
    (bundle_dir / "spec.yaml").write_text("name: y\n", encoding="utf-8")
    (git_repo / "README.md").write_text("hello\nworld\n", encoding="utf-8")

    p = compute_local_presence(bundle_dir)
    paths = [f.path for f in p.files]
    assert "spec.yaml" in paths
    assert "README.md" not in paths  # filtered: outside the bundle


def test_compute_local_presence_to_payload_round_trip(git_repo):
    """``LocalPresence.to_payload`` must produce the same shape the
    server expects. Round-trip via ``PresencePayload.to_json`` is the
    cheap check that field names didn't drift."""
    (git_repo / "README.md").write_text("hello\nworld\n", encoding="utf-8")
    p = compute_local_presence(git_repo)
    body = p.to_payload().to_json()
    assert "files" in body
    assert body["files"][0]["path"] == "README.md"
    assert "head_commit" in body
    assert "is_clean" in body


# ── PresenceCache ──────────────────────────────────────────────────


def test_presence_cache_apply_event_adds_peer():
    cache = PresenceCache()
    event = _make_incoming_presence_event(
        user_id=42,
        handle="alice",
        files=[PresenceFile(path="auth.py", lines_added=10, lines_removed=0)],
    )
    changed = cache.apply_event(event)
    assert changed is True
    assert len(cache) == 1


def test_presence_cache_apply_clean_state_drops_peer():
    """A teammate sending an explicit ``is_clean=True`` event must be
    removed from the cache so we stop showing them. This is how
    "shutdown" presence events tell the rest of the team you're no
    longer editing."""
    cache = PresenceCache()
    cache.apply_event(
        _make_incoming_presence_event(
            user_id=42,
            handle="alice",
            files=[PresenceFile(path="auth.py", lines_added=10, lines_removed=0)],
        )
    )
    assert len(cache) == 1
    cache.apply_event(
        _make_incoming_presence_event(
            user_id=42, handle="alice", files=[], is_clean=True, event_id=2,
            received_at=datetime.now(timezone.utc),
        )
    )
    assert len(cache) == 0


def test_presence_cache_ignores_only_this_install_echo():
    """The local SSE echo is not a teammate, but another machine on the
    same Spec account is. Identity therefore includes the broadcast client,
    not only the user id.
    """
    cache = PresenceCache(
        self_user_id=42,
        self_broadcast_client_id="this-install",
    )
    local_echo = _make_incoming_presence_event(
        user_id=42,
        handle="alice",
        files=[PresenceFile(path="auth.py", lines_added=1, lines_removed=0)],
        broadcast_client_id="this-install",
    )
    other_machine = _make_incoming_presence_event(
        user_id=42,
        handle="alice",
        files=[PresenceFile(path="billing.py", lines_added=1, lines_removed=0)],
        broadcast_client_id="laptop-two",
        event_id=2,
    )

    assert cache.apply_event(local_echo) is False
    assert cache.apply_event(other_machine) is True
    assert [peer.broadcast_client_id for peer in cache.current()] == ["laptop-two"]


def test_presence_cache_keeps_two_installations_for_one_account():
    cache = PresenceCache()
    for event_id, client, path in (
        (1, "laptop-one", "auth.py"),
        (2, "laptop-two", "billing.py"),
    ):
        cache.apply_event(
            _make_incoming_presence_event(
                user_id=42,
                handle="alice",
                files=[PresenceFile(path=path, lines_added=1, lines_removed=0)],
                event_id=event_id,
                broadcast_client_id=client,
            )
        )

    assert {peer.broadcast_client_id for peer in cache.current()} == {
        "laptop-one",
        "laptop-two",
    }


def test_presence_cache_skips_non_presence_events():
    cache = PresenceCache()
    event = _make_incoming_presence_event(user_id=1, handle="x", files=[])
    event.role = "user"
    assert cache.apply_event(event) is False
    assert len(cache) == 0


def test_presence_cache_skips_out_of_order_event():
    """A late-arriving presence event from before the freshest one we
    already have must not overwrite. Out-of-order delivery is rare on
    SSE but possible after a reconnect-driven replay.

    Note: ``current()`` calls ``expire_stale()`` against the freshness
    window, so we use recent timestamps relative to wall-clock — the
    point of this test is *ordering*, not staleness.
    """
    cache = PresenceCache()
    now = datetime.now(timezone.utc)
    newer = _make_incoming_presence_event(
        user_id=1,
        handle="alice",
        files=[PresenceFile(path="a.py", lines_added=2, lines_removed=0)],
        received_at=now,
    )
    older = _make_incoming_presence_event(
        user_id=1,
        handle="alice",
        files=[PresenceFile(path="b.py", lines_added=1, lines_removed=0)],
        received_at=now - timedelta(seconds=30),
    )
    cache.apply_event(newer)
    assert cache.apply_event(older) is False
    rendered = cache.current()
    assert rendered[0].files[0].path == "a.py"


def test_presence_cache_expire_stale():
    cache = PresenceCache(freshness_secs=1)
    cache.apply_event(
        _make_incoming_presence_event(
            user_id=1,
            handle="alice",
            files=[PresenceFile(path="a.py", lines_added=1, lines_removed=0)],
            received_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
    )
    assert len(cache) == 1
    removed = cache.expire_stale()
    assert removed == 1
    assert len(cache) == 0


def test_presence_cache_current_sorts_by_recent_first():
    cache = PresenceCache()
    older = _make_incoming_presence_event(
        user_id=1,
        handle="alice",
        files=[PresenceFile(path="a.py", lines_added=1, lines_removed=0)],
        received_at=datetime.now(timezone.utc),
    )
    newer = _make_incoming_presence_event(
        user_id=2,
        handle="bob",
        files=[PresenceFile(path="b.py", lines_added=1, lines_removed=0)],
        received_at=datetime.now(timezone.utc).replace(microsecond=999999),
    )
    cache.apply_event(older)
    cache.apply_event(newer)
    rendered = cache.current()
    assert rendered[0].handle == "bob"
    assert rendered[1].handle == "alice"


# ── TeamPresenceMirror ─────────────────────────────────────────────


def test_team_presence_mirror_writes_canonical_shape(tmp_path):
    cache = PresenceCache()
    cache.apply_event(
        _make_incoming_presence_event(
            user_id=1,
            handle="alice",
            files=[
                PresenceFile(path="auth.py", lines_added=12, lines_removed=3),
                PresenceFile(path="README.md", lines_added=1, lines_removed=0),
            ],
        )
    )
    local = LocalPresence(
        files=[
            PresenceFile(path="schemas.py", lines_added=5, lines_removed=2),
        ],
        head_commit="def456",
    )
    mirror = TeamPresenceMirror(tmp_path)
    changed = mirror.write(
        cache,
        local=local,
        self_handle="me",
        self_name="Me",
        branch="main",
    )
    assert changed is True

    body = read_team_presence(tmp_path)
    assert body is not None
    assert body["schema"] == 1
    assert body["self"]["handle"] == "me"
    assert len(body["members"]) == 1
    assert body["members"][0]["handle"] == "alice"
    assert "files_index" in body
    # Inverted index: peer's files indexed without ``self``, local's with ``self: true``.
    auth = body["files_index"]["auth.py"]
    assert any(e["handle"] == "alice" and e["self"] is False for e in auth)
    schemas = body["files_index"]["schemas.py"]
    assert any(e["handle"] == "me" and e["self"] is True for e in schemas)

    brief = tmp_path / ".spec" / TEAM_EDITING_BRIEF_FILENAME
    assert brief.is_file()
    assert "auth.py" in brief.read_text(encoding="utf-8")


def test_team_presence_mirror_idempotent_when_unchanged(tmp_path):
    cache = PresenceCache()
    cache.apply_event(
        _make_incoming_presence_event(
            user_id=1,
            handle="alice",
            files=[PresenceFile(path="x", lines_added=1, lines_removed=0)],
        )
    )
    mirror = TeamPresenceMirror(tmp_path)
    assert mirror.write(cache, local=None, self_handle=None, self_name=None, branch=None) is True
    # Second write with the same cache — must be a no-op (returns False).
    assert mirror.write(cache, local=None, self_handle=None, self_name=None, branch=None) is False


def test_team_presence_mirror_refreshes_health_timestamp(tmp_path):
    """An unchanged working tree must not make a running watcher look stale."""
    cache = PresenceCache()
    mirror = TeamPresenceMirror(tmp_path)
    first = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    later = first + timedelta(seconds=TEAM_PRESENCE_HEARTBEAT_SECS + 1)

    assert mirror.write(
        cache,
        local=None,
        self_handle=None,
        self_name=None,
        branch=None,
        now=first,
    )
    assert mirror.write(
        cache,
        local=None,
        self_handle=None,
        self_name=None,
        branch=None,
        now=later,
    )
    body = read_team_presence(tmp_path)
    assert body is not None
    assert body["updated_at"] == later.isoformat()


def test_read_team_presence_missing_file_returns_none(tmp_path):
    assert read_team_presence(tmp_path) is None


def test_read_team_presence_malformed_file_returns_none(tmp_path):
    (tmp_path / ".spec").mkdir()
    (tmp_path / ".spec" / "team-presence.json").write_text("not json", encoding="utf-8")
    assert read_team_presence(tmp_path) is None


def test_team_presence_mirror_writes_atomically(tmp_path):
    """Sanity: the temp file used during the write should be cleaned
    up. A failing test here would surface as ``team-presence.json.*.tmp``
    files left behind in ``.spec/``."""
    cache = PresenceCache()
    cache.apply_event(
        _make_incoming_presence_event(
            user_id=1,
            handle="alice",
            files=[PresenceFile(path="a", lines_added=1, lines_removed=0)],
        )
    )
    mirror = TeamPresenceMirror(tmp_path)
    mirror.write(cache, local=None, self_handle=None, self_name=None, branch=None)
    spec_dir = tmp_path / ".spec"
    leftover = list(spec_dir.glob("team-presence.json.*.tmp"))
    assert leftover == []
    written = (spec_dir / "team-presence.json").read_text(encoding="utf-8")
    parsed = json.loads(written)
    assert parsed["schema"] == 1
