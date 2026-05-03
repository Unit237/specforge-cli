"""
Tests for the v0.2 "one .prompts file per branch" capture behaviour.

These pin the contract that:

  - capture writes to ``prompts/<branch-slug>.prompts`` at the root of
    the prompts directory (not into ``prompts/captured/<timestamp>``)
  - capture is append-only, deduplicated by session id
  - the renderer round-trips the per-session ``[sessions.commit]``
    block plus the new ``merged_from`` / ``approved_by`` fields
  - the slugger handles common branch shapes (main, ``feature/x``,
    branches with case, branches with dotted names) without losing
    the original name (which lives in ``[commit].branch``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spec_cli.commands.prompts import (
    _branch_prompts_path,
    _merge_into_branch_file,
)
from spec_cli.prompts import (
    PromptsFile,
    Session,
    SessionCommit,
    Turn,
    read_prompts_file,
)
from spec_cli.prompts.render import (
    branch_prompts_filename,
    render_prompts_file,
)


def _make_session(sid: str, *, text: str = "hello") -> Session:
    return Session(
        id=sid,
        source="manual",
        turns=[Turn(role="user", text=text)],
    )


def test_branch_prompts_filename_slugger() -> None:
    assert branch_prompts_filename("main") == "main.prompts"
    assert branch_prompts_filename("Main") == "main.prompts"
    assert (
        branch_prompts_filename("feature/billing-rewrite")
        == "feature-billing-rewrite.prompts"
    )
    assert (
        branch_prompts_filename("dependabot/npm/foo-1.2.3")
        == "dependabot-npm-foo-1.2.3.prompts"
    )
    # Edge: only-junk → safe fallback rather than `prompts/.prompts`.
    assert branch_prompts_filename("///") == "branch.prompts"
    assert branch_prompts_filename("") == "branch.prompts"


def test_branch_prompts_path_lives_at_root_of_prompts_dir(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    p = _branch_prompts_path(bundle, "feature/x")
    assert p == bundle / "prompts" / "feature-x.prompts"


def test_capture_appends_idempotently(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "prompts").mkdir(parents=True)
    dest = _branch_prompts_path(bundle, "main")

    s1 = _make_session("aaa")
    n, ids = _merge_into_branch_file(
        dest,
        branch="main",
        author_name="Alice",
        author_email="alice@example.com",
        new_sessions=[s1],
    )
    assert n == 1
    assert ids == frozenset({"aaa"})
    assert dest.exists()

    # Same session id, second time around — must be a no-op write.
    n, ids = _merge_into_branch_file(
        dest,
        branch="main",
        author_name="Alice",
        author_email="alice@example.com",
        new_sessions=[s1],
    )
    assert n == 0
    assert ids == frozenset()
    pf = read_prompts_file(dest)
    assert len(pf.sessions) == 1


def test_capture_appends_new_sessions_in_started_at_order(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "prompts").mkdir(parents=True)
    dest = _branch_prompts_path(bundle, "main")

    s1 = _make_session("a")
    s2 = _make_session("b")
    s3 = _make_session("c")

    _merge_into_branch_file(
        dest,
        branch="main",
        author_name="Alice",
        author_email="alice@example.com",
        new_sessions=[s1],
    )
    n, _ids = _merge_into_branch_file(
        dest,
        branch="main",
        author_name="Alice",
        author_email="alice@example.com",
        new_sessions=[s2, s3],
    )
    assert n == 2

    pf = read_prompts_file(dest)
    ids = [s.id for s in pf.sessions]
    assert ids == ["a", "b", "c"]


def test_render_round_trip_new_v02_fields(tmp_path: Path) -> None:
    """Schema additions in v0.2 (per-session ``[sessions.commit]``,
    ``merged_from`` / ``merged_at`` / ``approved_by``) round-trip
    cleanly through the renderer."""
    from datetime import datetime, timezone

    pf = PromptsFile(
        commit=__import__(
            "spec_cli.prompts.schema", fromlist=["CommitMeta"]
        ).CommitMeta(
            branch="main",
            author_name="Alice",
            author_email="alice@example.com",
        ),
        sessions=[
            Session(
                id="sess-1",
                source="manual",
                commit=SessionCommit(
                    branch="feature/billing",
                    commit_sha="deadbeef",
                    author_username="alice",
                ),
                merged_from="feature/billing",
                merged_at=datetime(2026, 4, 30, 9, 0, 0, tzinfo=timezone.utc),
                approved_by="@bob",
                turns=[Turn(role="user", text="hi")],
            )
        ],
    )
    body = render_prompts_file(pf)
    p = tmp_path / "main.prompts"
    p.write_text(body, encoding="utf-8")

    parsed = read_prompts_file(p)
    s = parsed.sessions[0]
    assert s.merged_from == "feature/billing"
    assert s.approved_by == "@bob"
    assert s.merged_at == datetime(2026, 4, 30, 9, 0, 0, tzinfo=timezone.utc)
    assert s.commit is not None
    assert s.commit.branch == "feature/billing"
    assert s.commit.commit_sha == "deadbeef"
    assert s.commit.author_username == "alice"

    # Byte-determinism: re-rendering parses the same file and produces
    # the same bytes (the v0.1 contract; preserved through v0.2).
    assert render_prompts_file(parsed) == body


@pytest.mark.parametrize(
    "branch,expected_basename",
    [
        ("main", "main"),
        ("master", "master"),
        ("trunk", "trunk"),
        ("feature/billing-rewrite", "feature-billing-rewrite"),
        ("Bug/Fix.Race", "bug-fix.race"),
    ],
)
def test_branch_paths(tmp_path: Path, branch: str, expected_basename: str) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    p = _branch_prompts_path(bundle, branch)
    assert p.name == f"{expected_basename}.prompts"


def test_capture_replaces_session_when_turn_count_grew(tmp_path: Path) -> None:
    """The headline capture bug from the user report: a long-running
    Cursor / Claude Code session got captured once with N turns, the
    user kept typing, and every subsequent commit-msg hook reported
    "No new sessions to capture" — because the session id was already
    in the file. The merge now replaces the entry whenever the live
    transcript has grown, so the branch file always carries the freshest
    snapshot of every conversation that touched the bundle.
    """
    bundle = tmp_path / "bundle"
    (bundle / "prompts").mkdir(parents=True)
    dest = _branch_prompts_path(bundle, "main")

    short = Session(
        id="ongoing",
        source="cursor",
        turns=[
            Turn(role="user", text="first question"),
            Turn(role="assistant", summary="first reply"),
        ],
    )
    n1, ids1 = _merge_into_branch_file(
        dest,
        branch="main",
        author_name="Alice",
        author_email="alice@example.com",
        new_sessions=[short],
    )
    assert n1 == 1
    assert ids1 == frozenset({"ongoing"})

    grown = Session(
        id="ongoing",
        source="cursor",
        turns=[
            Turn(role="user", text="first question"),
            Turn(role="assistant", summary="first reply"),
            Turn(role="user", text="follow-up"),
            Turn(role="assistant", summary="second reply"),
        ],
    )
    n2, ids2 = _merge_into_branch_file(
        dest,
        branch="main",
        author_name="Alice",
        author_email="alice@example.com",
        new_sessions=[grown],
    )
    assert n2 == 1
    assert ids2 == frozenset({"ongoing"})

    pf = read_prompts_file(dest)
    assert len(pf.sessions) == 1
    assert len(pf.sessions[0].turns) == 4


def test_capture_keeps_existing_session_when_turn_count_shrank(
    tmp_path: Path,
) -> None:
    """Defensive: a re-ordered or partial source read shouldn't *truncate*
    the captured snapshot. We only replace when the new transcript has
    strictly more turns than the captured one."""
    bundle = tmp_path / "bundle"
    (bundle / "prompts").mkdir(parents=True)
    dest = _branch_prompts_path(bundle, "main")

    full = Session(
        id="ongoing",
        source="cursor",
        turns=[
            Turn(role="user", text="q1"),
            Turn(role="assistant", summary="r1"),
            Turn(role="user", text="q2"),
        ],
    )
    _merge_into_branch_file(
        dest,
        branch="main",
        author_name="Alice",
        author_email="alice@example.com",
        new_sessions=[full],
    )
    short = Session(
        id="ongoing",
        source="cursor",
        turns=[Turn(role="user", text="q1")],
    )
    n, ids = _merge_into_branch_file(
        dest,
        branch="main",
        author_name="Alice",
        author_email="alice@example.com",
        new_sessions=[short],
    )
    assert n == 0
    assert ids == frozenset()
    pf = read_prompts_file(dest)
    assert len(pf.sessions[0].turns) == 3


def test_capture_appends_alongside_replacement(tmp_path: Path) -> None:
    """Mixed batch: one growing session + one brand-new session in the
    same merge. Both land in the file in one write."""
    bundle = tmp_path / "bundle"
    (bundle / "prompts").mkdir(parents=True)
    dest = _branch_prompts_path(bundle, "main")

    seed = Session(
        id="growing",
        source="cursor",
        turns=[Turn(role="user", text="q1")],
    )
    _merge_into_branch_file(
        dest,
        branch="main",
        author_name="Alice",
        author_email="alice@example.com",
        new_sessions=[seed],
    )

    grown = Session(
        id="growing",
        source="cursor",
        turns=[Turn(role="user", text="q1"), Turn(role="user", text="q2")],
    )
    fresh = Session(
        id="brand-new",
        source="cursor",
        turns=[Turn(role="user", text="hello")],
    )
    n, ids = _merge_into_branch_file(
        dest,
        branch="main",
        author_name="Alice",
        author_email="alice@example.com",
        new_sessions=[grown, fresh],
    )
    assert n == 2
    assert ids == frozenset({"growing", "brand-new"})

    pf = read_prompts_file(dest)
    assert {s.id for s in pf.sessions} == {"growing", "brand-new"}
    by_id = {s.id: s for s in pf.sessions}
    assert len(by_id["growing"].turns) == 2
    assert len(by_id["brand-new"].turns) == 1
