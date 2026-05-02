"""Tests for pending-commit SHA prediction (commit-msg hook support)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from spec_cli.git import predict_commit_object_sha


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


def test_predict_commit_object_sha_matches_initial_commit(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@test")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "f.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "f.txt")

    msg = b"hello\n"
    predicted = predict_commit_object_sha(repo, msg)
    assert predicted is not None

    _git(repo, "commit", "--no-verify", "-m", "hello")
    actual = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert predicted == actual


def test_predict_commit_object_sha_matches_second_commit(tmp_path: Path) -> None:
    repo = tmp_path / "r2"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@test")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "f.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "--no-verify", "-m", "first")

    (repo / "f.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    msg = b"second\n"
    predicted = predict_commit_object_sha(repo, msg)
    assert predicted is not None

    _git(repo, "commit", "--no-verify", "-m", "second")
    actual = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert predicted == actual
