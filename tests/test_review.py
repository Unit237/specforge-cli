from __future__ import annotations

import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from spec_cli.commands import review
from spec_cli.git import GitContext


def _completed(args: list[str], *, body: dict | None = None, returncode: int = 0):
    return subprocess.CompletedProcess(
        args,
        returncode,
        stdout=json.dumps(body) if body is not None else "",
        stderr="",
    )


def _setup(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(review, "find_bundle_root", lambda: tmp_path)
    monkeypatch.setattr(review.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        review,
        "read_git_context",
        lambda _root: GitContext(
            is_repo=True,
            github_repository="lightreach/app",
            origin_url="git@github.com:lightreach/app.git",
        ),
    )


def test_review_adds_explicit_agent_review_label(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    calls: list[list[str]] = []

    def run(arguments, *, cwd):
        assert cwd == tmp_path
        calls.append(arguments)
        if arguments[:2] == ["pr", "view"]:
            return _completed(
                arguments,
                body={
                    "number": 42,
                    "url": "https://github.com/lightreach/app/pull/42",
                    "headRefOid": "a" * 40,
                    "isDraft": False,
                    "state": "OPEN",
                    "labels": [],
                    "title": "Review me",
                },
            )
        if arguments[:2] == ["label", "list"]:
            return _completed(arguments, body=[])
        return _completed(arguments)

    monkeypatch.setattr(review, "_run_gh", run)
    result = CliRunner().invoke(review.review_cmd, ["--json"])

    assert result.exit_code == 0, result.output
    assert calls[2][:3] == ["label", "create", "agent-review"]
    assert calls[3] == [
        "pr",
        "edit",
        "42",
        "--repo",
        "lightreach/app",
        "--add-label",
        "agent-review",
    ]
    receipt = json.loads(result.output)
    assert receipt["requested"] is True
    assert receipt["pass_threshold"] == 9.0


def test_review_is_idempotent_when_label_is_present(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    calls: list[list[str]] = []

    def run(arguments, *, cwd):
        calls.append(arguments)
        return _completed(
            arguments,
            body={
                "number": 7,
                "url": "https://github.com/lightreach/app/pull/7",
                "headRefOid": "b" * 40,
                "isDraft": False,
                "state": "OPEN",
                "labels": [{"name": "agent-review"}],
                "title": "Already requested",
            },
        )

    monkeypatch.setattr(review, "_run_gh", run)
    result = CliRunner().invoke(review.review_cmd, ["--json"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert json.loads(result.output)["already_requested"] is True


def test_review_rejects_draft_pull_request(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        review,
        "_run_gh",
        lambda arguments, *, cwd: _completed(
            arguments,
            body={
                "number": 3,
                "url": "https://github.com/lightreach/app/pull/3",
                "headRefOid": "c" * 40,
                "isDraft": True,
                "state": "OPEN",
                "labels": [],
                "title": "Draft",
            },
        ),
    )

    result = CliRunner().invoke(review.review_cmd)

    assert result.exit_code == 1
    assert "ready for review" in result.output


def test_prs_lists_open_requests_across_watched_repositories(monkeypatch, tmp_path):
    monkeypatch.setattr(review.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        review,
        "_watched_repositories",
        lambda: [(tmp_path, "lightreach/app"), (tmp_path, "lightreach/api")],
    )

    def run(arguments, *, cwd):
        repository = arguments[arguments.index("--repo") + 1]
        return _completed(
            arguments,
            body=[
                {
                    "number": 4 if repository.endswith("app") else 9,
                    "url": f"https://github.com/{repository}/pull/1",
                    "headRefOid": "d" * 40,
                    "isDraft": False,
                    "state": "OPEN",
                    "labels": [],
                    "title": repository,
                    "author": {"login": "jon"},
                }
            ],
        )

    monkeypatch.setattr(review, "_run_gh", run)
    result = CliRunner().invoke(review.prs_cmd, ["--json"])

    assert result.exit_code == 0, result.output
    pulls = json.loads(result.output)
    assert [(row["repository"], row["number"]) for row in pulls] == [
        ("lightreach/api", 9),
        ("lightreach/app", 4),
    ]


def test_review_requests_native_github_review_from_spec_teammate(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    calls: list[list[str]] = []

    def run(arguments, *, cwd):
        calls.append(arguments)
        if arguments[:2] == ["pr", "view"]:
            return _completed(
                arguments,
                body={
                    "number": 12,
                    "url": "https://github.com/lightreach/app/pull/12",
                    "headRefOid": "e" * 40,
                    "isDraft": False,
                    "state": "OPEN",
                    "labels": [],
                    "title": "Team review",
                    "author": {"login": "jon"},
                },
            )
        return _completed(arguments)

    monkeypatch.setattr(review, "_run_gh", run)
    monkeypatch.setattr(
        review,
        "_team_reviewers",
        lambda _root: [
            {"handle": "alice", "github_login": "alice-gh", "email": "alice@example.com"}
        ],
    )

    result = CliRunner().invoke(review.review_cmd, ["--with", "@alice", "--json"])

    assert result.exit_code == 0, result.output
    assert calls[-1] == [
        "pr",
        "edit",
        "12",
        "--repo",
        "lightreach/app",
        "--add-reviewer",
        "alice-gh",
    ]
    assert json.loads(result.output)["reviewer"] == "alice-gh"
