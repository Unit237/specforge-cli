"""Tests for ``spec locks pull-status`` — the post-push pull-needed hint.

These exercise the wiring between ``team-presence.json``,
:func:`_compute_pull_alerts` and the CLI exit-code contract:

* exit 0 when the mirror is missing, stale, or no peers are ahead
* exit 2 when at least one teammate on the same branch has a
  different ``head_commit`` (fresh mirror)
* ``--json`` always emits a parseable object with ``clear`` and
  ``alerts`` keys

This is the contract AI IDE hooks and pre-edit rules depend on.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from spec_cli.commands.locks import locks_group
from spec_cli.realtime.coordination import CoordinationCache, TeamCoordinationMirror


def _write_presence(root: Path, body: dict) -> None:
    spec = root / ".spec"
    spec.mkdir(parents=True, exist_ok=True)
    (spec / "team-presence.json").write_text(json.dumps(body), encoding="utf-8")
    TeamCoordinationMirror(root).sync(
        CoordinationCache(root),
        now=datetime.fromisoformat(str(body["updated_at"])),
    )


def _fresh_body(self_commit: str, peer_commit: str, *,
                self_branch: str = "main",
                peer_branch: str = "main",
                peer_handle: str = "alice") -> dict:
    return {
        "schema": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "self": {
            "handle": "jon",
            "name": "Jon",
            "branch": self_branch,
            "head_commit": self_commit,
            "files": [],
        },
        "members": [
            {
                "handle": peer_handle,
                "name": peer_handle.capitalize(),
                "branch": peer_branch,
                "head_commit": peer_commit,
                "last_seen": datetime.now(timezone.utc).isoformat(),
                "files": [],
            }
        ],
        "files_index": {},
    }


@pytest.fixture
def bundle(tmp_path: Path, monkeypatch) -> Path:
    """Make ``tmp_path`` look like a valid Spec bundle so
    ``find_bundle_root`` resolves into it from any cwd."""
    (tmp_path / "spec.yaml").write_text("name: test\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_pull_status_clear_when_no_presence_file(bundle):
    runner = CliRunner()
    result = runner.invoke(locks_group, ["pull-status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["clear"] is True
    assert payload["reason"] == "no_live_data"


def test_pull_status_clear_when_mirror_is_stale(bundle):
    """A stale mirror means ``spec watch`` isn't running — fail open."""
    stale_body = _fresh_body("aaa111aaa", "bbb222bbb")
    stale_body["updated_at"] = "2000-01-01T00:00:00+00:00"
    _write_presence(bundle, stale_body)

    runner = CliRunner()
    result = runner.invoke(locks_group, ["pull-status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["clear"] is True
    assert payload["reason"] == "stale_mirror"


def test_pull_status_fires_when_peer_is_ahead(bundle):
    """Canonical case: @alice pushed to main, we're still on the
    prior commit. Exit 2 + alert in JSON output."""
    body = _fresh_body("aaa111aaa", "bbb222bbb")
    _write_presence(bundle, body)

    runner = CliRunner()
    result = runner.invoke(locks_group, ["pull-status", "--json"])
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["clear"] is False
    assert len(payload["alerts"]) == 1
    alert = payload["alerts"][0]
    assert alert["handle"] == "alice"
    assert alert["branch"] == "main"
    assert alert["short_commit"] == "bbb222b"
    assert alert["self_short"] == "aaa111a"


def test_pull_status_clear_when_peer_on_different_branch(bundle):
    """Cross-branch divergence is normal and should NOT trip the alert."""
    body = _fresh_body(
        "aaa111aaa",
        "bbb222bbb",
        self_branch="main",
        peer_branch="feature/auth",
    )
    _write_presence(bundle, body)

    runner = CliRunner()
    result = runner.invoke(locks_group, ["pull-status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["clear"] is True
    assert payload["alerts"] == []


def test_pull_status_clear_when_peer_in_sync(bundle):
    """Same branch, same SHA — peer is in sync."""
    body = _fresh_body("aaa111aaa", "aaa111aaa")
    _write_presence(bundle, body)

    runner = CliRunner()
    result = runner.invoke(locks_group, ["pull-status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["clear"] is True


def test_pull_status_human_output_mentions_git_pull(bundle):
    """Non-JSON output should contain a "git pull" hint a human can act on."""
    body = _fresh_body("aaa111aaa", "bbb222bbb")
    _write_presence(bundle, body)

    runner = CliRunner()
    result = runner.invoke(locks_group, ["pull-status"])
    assert result.exit_code == 2
    assert "pull needed" in result.output.lower()
    assert "git pull" in result.output
    assert "@alice" in result.output


def test_locks_check_includes_pull_alerts_in_json(bundle):
    """``spec locks check`` JSON output now carries ``pull_alerts``
    alongside the per-path ``holders``, so hooks can react to both."""
    body = _fresh_body("aaa111aaa", "bbb222bbb")
    _write_presence(bundle, body)

    runner = CliRunner()
    result = runner.invoke(locks_group, ["check", "some/path.py", "--json"])
    assert result.exit_code == 0  # no overlap on the path
    payload = json.loads(result.output)
    assert payload["clear"] is True
    assert "pull_alerts" in payload
    assert len(payload["pull_alerts"]) == 1
    assert payload["pull_alerts"][0]["handle"] == "alice"
