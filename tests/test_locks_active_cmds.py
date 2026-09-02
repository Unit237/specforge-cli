"""End-to-end tests for the new ``spec locks acquire/release/list/prune``
commands and the ``spec locks check`` integration with active-edits.

These run the real ``spec`` binary as a subprocess so we catch the
exit-code contract (which AI hooks rely on) in addition to the JSON
output shape. The subprocess form also exercises the cross-process
file lock — every test invocation is a fresh PID, so a leaked
``flock`` would surface as a hang.

Contracts under test:

* ``spec locks acquire`` writes the lock file synchronously and
  prints a usable lock id in both human and JSON mode.
* Acquiring an overlapping lock as a different agent surfaces a
  conflict in JSON output and triggers exit ``2`` under ``--block``.
* ``spec locks release <id>`` removes the lock. An unknown id
  exits ``0`` (silent no-op) because PostToolUse must never fail.
* ``spec locks list`` reflects the file shape and respects the
  ``--agent`` / ``--session`` filters.
* ``spec locks check <path> --json`` includes the active-edit
  holders alongside team-presence holders.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from spec_cli.realtime.coordination import CoordinationCache, TeamCoordinationMirror


SPEC_BIN = [sys.executable, "-m", "spec_cli"]
SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(autouse=True)
def isolate_spec_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))


def _active_file() -> Path:
    return Path(os.environ["SPEC_HOME"]) / "active-edits.json"


def _make_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir(parents=True)
    (bundle / "spec.yaml").write_text("name: demo\n", encoding="utf-8")
    return bundle


def _write_presence(root: Path, *, updated_at: datetime | None = None) -> None:
    spec_dir = root / ".spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    at = updated_at or datetime.now(timezone.utc)
    (spec_dir / "team-presence.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "updated_at": at.isoformat(),
                "self": None,
                "members": [],
                "files_index": {},
            }
        ),
        encoding="utf-8",
    )
    TeamCoordinationMirror(root).sync(CoordinationCache(root), now=at)


def _run(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    prior_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(SOURCE_ROOT)
        if not prior_pythonpath
        else str(SOURCE_ROOT) + os.pathsep + prior_pythonpath
    )
    return subprocess.run(
        SPEC_BIN + args,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )


def test_acquire_creates_lock_file(tmp_path: Path) -> None:
    """A successful acquire creates the machine-wide registry and
    prints a lock id the caller can use for release."""
    bundle = _make_bundle(tmp_path)
    res = _run(
        [
            "locks",
            "acquire",
            "src/auth.py",
            "--agent",
            "claude_code",
            "--session",
            "abc",
            "--intent",
            "Edit",
            "--json",
        ],
        cwd=bundle,
    )
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert payload["acquired"] is True
    assert payload["agent"] == "claude_code"
    assert payload["session_id"] == "abc"
    assert payload["paths"] == ["src/auth.py"]
    assert payload["lock_id"]
    assert payload["conflicts"] == []
    assert payload["bundle_root"] == str(bundle.resolve())
    on_disk = json.loads(_active_file().read_text(encoding="utf-8"))
    assert len(on_disk["locks"]) == 1
    assert not (bundle / ".spec" / "active-edits.json").exists()


def test_acquire_block_mode_exits_two_on_conflict(tmp_path: Path) -> None:
    """``--block`` is what AI hooks pass when the team wants firm
    coordination. A cross-agent overlap → exit 2, surfacing as a
    refused tool call in Claude Code / Cursor's hook chain."""
    bundle = _make_bundle(tmp_path)
    r1 = _run(
        ["locks", "acquire", "auth.py", "--agent", "claude_code", "--session", "a", "--json"],
        cwd=bundle,
    )
    assert r1.returncode == 0
    r2 = _run(
        ["locks", "acquire", "auth.py", "--agent", "cursor", "--session", "b", "--block", "--json"],
        cwd=bundle,
    )
    assert r2.returncode == 2
    body = json.loads(r2.stdout)
    assert body["acquired"] is True
    assert len(body["conflicts"]) == 1
    assert body["conflicts"][0]["agent"] == "claude_code"
    assert body["conflicts"][0]["overlapping_paths"] == ["auth.py"]


def test_renewal_is_not_a_conflict(tmp_path: Path) -> None:
    """Same agent + session re-acquire is a renewal. The block-mode
    flag should still exit 0 because there's no conflict — the
    caller is renewing its own lock, not stomping on someone else's."""
    bundle = _make_bundle(tmp_path)
    _run(
        ["locks", "acquire", "auth.py", "--agent", "claude_code", "--session", "a", "--json"],
        cwd=bundle,
    )
    res = _run(
        [
            "locks",
            "acquire",
            "auth.py",
            "--agent",
            "claude_code",
            "--session",
            "a",
            "--block",
            "--json",
        ],
        cwd=bundle,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    body = json.loads(res.stdout)
    assert body["conflicts"] == []


def test_release_removes_lock(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    acquired = json.loads(
        _run(
            ["locks", "acquire", "auth.py", "--agent", "cursor", "--json"],
            cwd=bundle,
        ).stdout
    )
    lock_id = acquired["lock_id"]
    res = _run(
        ["locks", "release", lock_id, "--json"],
        cwd=bundle,
    )
    assert res.returncode == 0
    body = json.loads(res.stdout)
    assert body["released"] is True

    listed = json.loads(_run(["locks", "list", "--json"], cwd=bundle).stdout)
    assert listed["locks"] == []


def test_release_unknown_id_is_noop(tmp_path: Path) -> None:
    """PostToolUse must never fail on an unknown id — the matching
    PreToolUse hook may have crashed, expired, or simply never
    fired. Silent exit 0 keeps Claude / Cursor unblocked."""
    bundle = _make_bundle(tmp_path)
    res = _run(
        ["locks", "release", "00000000-0000-0000-0000-000000000000", "--json"],
        cwd=bundle,
    )
    assert res.returncode == 0
    body = json.loads(res.stdout)
    assert body["released"] is False


def test_list_filters_by_agent(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    _run(
        ["locks", "acquire", "a.py", "--agent", "claude_code", "--session", "1", "--json"],
        cwd=bundle,
    )
    _run(["locks", "acquire", "b.py", "--agent", "cursor", "--session", "2", "--json"], cwd=bundle)

    res = _run(
        ["locks", "list", "--agent", "cursor", "--json"],
        cwd=bundle,
    )
    assert res.returncode == 0
    body = json.loads(res.stdout)
    assert len(body["locks"]) == 1
    assert body["locks"][0]["agent"] == "cursor"


def test_check_surfaces_active_holders_in_json(tmp_path: Path) -> None:
    """The check command must report active-edit holders so an AI
    agent calling it sees "you have it locked from your other pane"
    even when no teammate is dirty."""
    bundle = _make_bundle(tmp_path)
    # Take a lock as "cursor"; then ask check about it.
    acquire = json.loads(
        _run(
            ["locks", "acquire", "auth.py", "--agent", "cursor", "--session", "s", "--json"],
            cwd=bundle,
        ).stdout
    )
    res = _run(
        ["locks", "check", "auth.py", "--json"],
        cwd=bundle,
    )
    # Active-edit holders bypass the team-presence freshness gate,
    # so exit code is whatever the check command uses for conflict
    # (non-zero) regardless of whether team-presence mirror exists.
    body = json.loads(res.stdout)
    assert body["path"] == "auth.py"
    # Some "holders" array surfaced the active-edit row.
    holders = body.get("holders") or []
    assert any(h.get("kind") == "active_edit" for h in holders)
    assert any(h.get("lock_id") == acquire["lock_id"] for h in holders)


def test_check_distinguishes_unknown_from_clear(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)

    result = _run(["locks", "check", "auth.py", "--json"], cwd=bundle)

    assert result.returncode == 3
    assert json.loads(result.stdout) == {
        "state": "unknown",
        "clear": False,
        "path": "auth.py",
        "holders": [],
        "pull_alerts": [],
        "reason": "no_live_data",
    }


def test_check_reports_clear_only_from_a_fresh_mirror(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    _write_presence(bundle)

    result = _run(["locks", "check", "auth.py", "--json"], cwd=bundle)

    assert result.returncode == 0
    assert json.loads(result.stdout)["state"] == "clear"
    assert json.loads(result.stdout)["clear"] is True


def test_check_never_false_clears_when_coordination_projection_is_missing(
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path)
    _write_presence(bundle)
    (bundle / ".spec" / "team-coordination-health.json").unlink()

    result = _run(["locks", "check", "auth.py", "--json"], cwd=bundle)

    assert result.returncode == 3
    body = json.loads(result.stdout)
    assert body["state"] == "unknown"
    assert body["reason"] == "no_coordination_data"


def test_check_reports_stale_mirror_as_unknown(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    _write_presence(bundle, updated_at=datetime.now(timezone.utc) - timedelta(hours=1))

    result = _run(["locks", "check", "auth.py", "--json"], cwd=bundle)

    assert result.returncode == 3
    assert json.loads(result.stdout)["reason"] == "stale_mirror"


def test_check_merges_live_task_claims_into_the_conflict_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _make_bundle(tmp_path)
    _write_presence(bundle)
    (bundle / ".spec" / "team-coordination.json").write_text(
        json.dumps(
            {
                "schema": "spec.team-coordination/v1",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "active": [],
                "recent_outcomes": [],
                "files_index": {
                    "auth.py": [
                        {
                            "kind": "task_claim",
                            "key": "1:client:codex:session-a",
                            "agent": "codex",
                            "author": "@alice",
                            "author_user_id": 1,
                            "session_id": "session-a",
                            "broadcast_client_id": "client",
                            "objective": "Build auth",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    result = _run(["locks", "check", "auth.py", "--json"], cwd=bundle)

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["state"] == "conflict"
    assert payload["holders"][0]["kind"] == "task_claim"
    assert payload["holders"][0]["session_id"] == "session-a"

    monkeypatch.setenv("CODEX_THREAD_ID", "session-a")
    own_result = _run(["locks", "check", "auth.py", "--json"], cwd=bundle)
    assert own_result.returncode == 0
    assert json.loads(own_result.stdout)["state"] == "clear"


def test_check_uses_existing_coordination_mirror_without_manifest(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_presence(repo)
    target = repo / "src" / "auth.py"
    target.parent.mkdir()
    target.write_text("", encoding="utf-8")

    result = _run(["locks", "check", "src/auth.py", "--json"], cwd=repo)

    assert result.returncode == 0
    assert json.loads(result.stdout)["state"] == "clear"


def test_acquire_uses_existing_coordination_mirror_without_manifest(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_presence(repo)

    result = _run(
        ["locks", "acquire", "src/auth.py", "--agent", "codex", "--json"],
        cwd=repo,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["bundle_root"] == str(repo.resolve())
    assert payload["paths"] == ["src/auth.py"]


def test_prune_removes_expired(tmp_path: Path) -> None:
    """Acquire a very short-TTL lock, sleep past it, prune — the
    file should be empty. We don't actually sleep; we rewrite
    expires_at to the past, which simulates a crashed agent."""
    bundle = _make_bundle(tmp_path)
    acquired = json.loads(
        _run(
            ["locks", "acquire", "auth.py", "--agent", "cursor", "--session", "s", "--json"],
            cwd=bundle,
        ).stdout
    )
    file = _active_file()
    body = json.loads(file.read_text(encoding="utf-8"))
    body["locks"][0]["expires_at"] = "2020-01-01T00:00:00+00:00"
    file.write_text(json.dumps(body), encoding="utf-8")

    res = _run(["locks", "prune", "--json"], cwd=bundle)
    assert res.returncode == 0
    assert json.loads(res.stdout) == {"pruned": 1}
    final = json.loads(file.read_text(encoding="utf-8"))
    assert final["locks"] == []
    # Quiet the unused warning.
    assert acquired["lock_id"]


def test_list_all_reads_every_bundle_from_outside_a_bundle(tmp_path: Path) -> None:
    first = _make_bundle(tmp_path / "one")
    second = _make_bundle(tmp_path / "two")
    _run(
        ["locks", "acquire", "same.py", "--agent", "codex", "--json"],
        cwd=first,
    )
    second_result = _run(
        ["locks", "acquire", "same.py", "--agent", "cursor", "--json"],
        cwd=second,
    )
    assert json.loads(second_result.stdout)["conflicts"] == []

    outside = tmp_path / "outside"
    outside.mkdir()
    result = _run(["locks", "list", "--all", "--json"], cwd=outside)

    assert result.returncode == 0, result.stderr
    locks = json.loads(result.stdout)["locks"]
    assert {row["bundle_root"] for row in locks} == {
        str(first.resolve()),
        str(second.resolve()),
    }
    assert {tuple(row["paths"]) for row in locks} == {("same.py",)}
