"""Tests for `spec push` auto-creating a Cloud bundle when the target is missing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from spec_cli.api import ApiError
from spec_cli.cli import cli
from spec_cli.config import Credentials


def _bundle(root: Path, *, cloud_project: str, name: str = "my-bundle") -> None:
    root.mkdir(parents=True)
    (root / "spec.yaml").write_text(
        yaml.safe_dump(
            {
                "schema": "spec/v0.1",
                "name": name,
                "cloud": {"project": cloud_project},
                "spec": {"entry": "docs/p.md", "include": ["docs/**/*.md"], "exclude": []},
            }
        ),
        encoding="utf-8",
    )
    (root / "docs").mkdir()
    (root / "docs" / "p.md").write_text("# p\n", encoding="utf-8")


def _fake_git_main(root: Path) -> None:
    """Minimal git metadata so read_git_context sees trunk."""
    import subprocess

    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True, capture_output=True)


@pytest.fixture
def mock_cloud_client() -> MagicMock:
    client = MagicMock()
    client.resolve_project.side_effect = ApiError(
        "Cloud API GET … → 400: Project `alice/my-bundle` not found",
        status=400,
        body={"detail": "Project `alice/my-bundle` not found"},
    )
    client.create_project.return_value = {
        "id": 42,
        "slug": "my-bundle",
        "owner_handle": "alice",
        "bundle_id": "bdl_auto_create_test",
        "default_branch": "main",
    }

    def _batch(_project_id: int, chunk: list, **kw):
        return {"results": [{"ok": True, "path": item["path"]} for item in chunk]}

    client.batch_upload.side_effect = _batch
    return client


def test_push_auto_creates_when_missing_and_handle_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_cloud_client: MagicMock
) -> None:
    root = tmp_path / "b"
    _bundle(root, cloud_project="alice/my-bundle")
    _fake_git_main(root)
    monkeypatch.chdir(root)

    runner = CliRunner()
    runner.invoke(cli, ["add", ".", "--no-capture"], catch_exceptions=False)

    creds = Credentials(
        api_base="https://spec.example",
        access_token="tok",
        user_handle="alice",
    )
    monkeypatch.setattr("spec_cli.commands.push.load_credentials", lambda: creds)

    with patch("spec_cli.commands.push.CloudClient", return_value=mock_cloud_client):
        r = runner.invoke(cli, ["push"], catch_exceptions=False)

    assert r.exit_code == 0
    mock_cloud_client.create_project.assert_called_once_with("my-bundle")
    mock_cloud_client.batch_upload.assert_called()


def test_push_updates_manifest_when_server_suffixes_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_cloud_client: MagicMock
) -> None:
    root = tmp_path / "b"
    _bundle(root, cloud_project="alice/my-bundle")
    _fake_git_main(root)
    monkeypatch.chdir(root)

    runner = CliRunner()
    runner.invoke(cli, ["add", ".", "--no-capture"], catch_exceptions=False)

    mock_cloud_client.create_project.return_value = {
        "id": 43,
        "slug": "my-bundle-2",
        "owner_handle": "alice",
        "bundle_id": "bdl_suffix_test",
        "default_branch": "main",
    }

    creds = Credentials(
        api_base="https://spec.example",
        access_token="tok",
        user_handle="alice",
    )
    monkeypatch.setattr("spec_cli.commands.push.load_credentials", lambda: creds)

    with patch("spec_cli.commands.push.CloudClient", return_value=mock_cloud_client):
        r = runner.invoke(cli, ["push"], catch_exceptions=False)

    assert r.exit_code == 0
    data = yaml.safe_load((root / "spec.yaml").read_text(encoding="utf-8"))
    assert data["cloud"]["project"] == "alice/my-bundle-2"


def test_push_does_not_auto_create_for_foreign_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_cloud_client: MagicMock
) -> None:
    root = tmp_path / "b"
    _bundle(root, cloud_project="bob/their-bundle")
    _fake_git_main(root)
    monkeypatch.chdir(root)

    runner = CliRunner()
    runner.invoke(cli, ["add", ".", "--no-capture"], catch_exceptions=False)

    creds = Credentials(
        api_base="https://spec.example",
        access_token="tok",
        user_handle="alice",
    )
    monkeypatch.setattr("spec_cli.commands.push.load_credentials", lambda: creds)

    mock_cloud_client.resolve_project.side_effect = ApiError(
        "nf",
        status=400,
        body={"detail": "Project `bob/their-bundle` not found"},
    )

    with patch("spec_cli.commands.push.CloudClient", return_value=mock_cloud_client):
        r = runner.invoke(cli, ["push"], catch_exceptions=False)

    assert r.exit_code != 0
    mock_cloud_client.create_project.assert_not_called()


def test_push_canonicalizes_bare_cloud_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bare `cloud.project` is rewritten to owner/slug from Cloud after resolve."""
    root = tmp_path / "bare"
    _bundle(root, cloud_project="my-bundle")
    _fake_git_main(root)
    monkeypatch.chdir(root)

    runner = CliRunner()
    runner.invoke(cli, ["add", ".", "--no-capture"], catch_exceptions=False)

    client = MagicMock()
    client.resolve_project.return_value = {
        "id": 77,
        "slug": "my-bundle",
        "owner_handle": "alice",
        "bundle_id": "bdl_bare_slug",
        "default_branch": "main",
    }

    def _batch(_project_id: int, chunk: list, **kw):
        return {"results": [{"ok": True, "path": item["path"]} for item in chunk]}

    client.batch_upload.side_effect = _batch

    creds = Credentials(
        api_base="https://spec.example",
        access_token="tok",
        user_handle="alice",
    )
    monkeypatch.setattr("spec_cli.commands.push.load_credentials", lambda: creds)

    with patch("spec_cli.commands.push.CloudClient", return_value=client):
        r = runner.invoke(cli, ["push"], catch_exceptions=False)

    assert r.exit_code == 0
    data = yaml.safe_load((root / "spec.yaml").read_text(encoding="utf-8"))
    assert data["cloud"]["project"] == "alice/my-bundle"


def test_push_binds_github_origin_and_defers_review_to_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    root = tmp_path / "github"
    _bundle(root, cloud_project="alice/my-bundle")
    _fake_git_main(root)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:Unit237/specforge.git"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True)
    subprocess.run(["git", "switch", "-c", "agent/github-sync"], cwd=root, check=True)
    monkeypatch.chdir(root)

    runner = CliRunner()
    runner.invoke(cli, ["add", ".", "--no-capture"], catch_exceptions=False)

    client = MagicMock()
    client.resolve_project.return_value = {
        "id": 88,
        "slug": "my-bundle",
        "owner_handle": "alice",
        "bundle_id": "bdl_github",
        "default_branch": "main",
    }

    def _batch(_project_id: int, chunk: list, **_kwargs):
        return {"results": [{"ok": True, "path": item["path"]} for item in chunk]}

    client.batch_upload.side_effect = _batch
    creds = Credentials(
        api_base="https://spec.example",
        access_token="tok",
        user_handle="alice",
    )
    monkeypatch.setattr("spec_cli.commands.push.load_credentials", lambda: creds)

    with patch("spec_cli.commands.push.CloudClient", return_value=client):
        result = runner.invoke(cli, ["push"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert any(
        call.kwargs.get("github_repository") == "Unit237/specforge"
        for call in client.batch_upload.call_args_list
    )
    client.open_branch_review.assert_not_called()
    assert "GitHub is canonical" in result.output
