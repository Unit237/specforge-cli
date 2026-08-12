"""Workspace Git repository discovery and bulk Spec initialization."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from spec_cli.cli import cli
from spec_cli.commands.discover import (
    discover_git_repositories,
    inspect_git_repositories,
    parse_repository_selection,
)
from spec_cli.preferences import load_preferences


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def test_discover_git_repositories_finds_nested_repos_and_prunes_dependencies(tmp_path):
    root_repo = _git_repo(tmp_path / "root")
    nested_repo = _git_repo(tmp_path / "root" / "services" / "api")
    ignored_repo = _git_repo(tmp_path / "root" / "node_modules" / "dependency")

    found = discover_git_repositories(tmp_path, max_depth=8)

    assert root_repo.resolve() in found
    assert nested_repo.resolve() in found
    assert ignored_repo.resolve() not in found


def test_discover_git_repositories_includes_containing_worktree(tmp_path):
    root = _git_repo(tmp_path / "repo")
    child = root / "src" / "feature"
    child.mkdir(parents=True)

    assert discover_git_repositories(child) == [root.resolve()]


def test_inspect_repositories_recognizes_untracked_root_manifest(tmp_path):
    root = _git_repo(tmp_path / "repo")
    (root / "spec.yaml").write_text(
        'schema: "spec/v0.1"\nname: demo\ncloud:\n  project: demo\n',
        encoding="utf-8",
    )

    inspected = inspect_git_repositories([root])

    assert inspected[0].initialized is True
    assert inspected[0].bundle_roots == (root,)


@pytest.mark.parametrize(
    ("raw", "count", "expected"),
    [
        ("", 3, [0, 1, 2]),
        ("all", 3, [0, 1, 2]),
        ("*", 2, [0, 1]),
        ("1, 3", 3, [0, 2]),
        ("1-3, 2", 4, [0, 1, 2]),
        ("none", 3, []),
        ("q", 3, []),
    ],
)
def test_parse_repository_selection(raw, count, expected):
    assert parse_repository_selection(raw, count) == expected


@pytest.mark.parametrize("raw", ["0", "4", "2-1", "one", "1-x"])
def test_parse_repository_selection_rejects_invalid_input(raw):
    with pytest.raises(ValueError):
        parse_repository_selection(raw, 3)


def test_discover_dry_run_lists_without_changing_repositories(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    repo = _git_repo(tmp_path / "workspace" / "api")

    result = CliRunner().invoke(cli, ["discover", str(tmp_path / "workspace"), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "[1]" in result.output
    assert "api · ready to initialize" in result.output
    assert "Dry run" in result.output
    assert not (repo / "spec.yaml").exists()


def test_discover_interactively_initializes_only_selected_repositories(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    workspace = tmp_path / "workspace"
    alpha = _git_repo(workspace / "alpha")
    bravo = _git_repo(workspace / "bravo")
    (bravo / "AGENTS.md").write_text(
        "# Existing team instructions\n\nKeep this.\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["discover", str(workspace)], input="2\n")

    assert result.exit_code == 0, result.output
    assert not (alpha / "spec.yaml").exists()
    assert (bravo / "spec.yaml").is_file()
    assert (bravo / ".cursor" / "rules" / "spec-team-presence.mdc").is_file()
    assert (bravo / ".claude" / "settings.json").is_file()
    agents = (bravo / "AGENTS.md").read_text(encoding="utf-8")
    assert "Keep this." in agents
    assert agents.count("<!-- >>> spec live coordination >>>") == 1
    assert load_preferences().bundles == [str(bravo.resolve())]
    assert "Initialized 1 repository" in result.output


def test_discover_all_initializes_every_available_repo_and_skips_existing(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    workspace = tmp_path / "workspace"
    alpha = _git_repo(workspace / "alpha")
    bravo = _git_repo(workspace / "bravo")
    existing = _git_repo(workspace / "existing")
    existing_manifest = existing / "spec.yaml"
    existing_manifest.write_text(
        'schema: "spec/v0.1"\nname: existing\ncloud:\n  project: existing\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["discover", str(workspace), "--all"])

    assert result.exit_code == 0, result.output
    assert (alpha / "spec.yaml").is_file()
    assert (bravo / "spec.yaml").is_file()
    assert existing_manifest.read_text(encoding="utf-8").startswith('schema: "spec/v0.1"')
    assert "existing · Spec initialized" in result.output
    assert "Initialized 2 repositories" in result.output
    prefs = load_preferences()
    assert set(prefs.bundles) == {
        str(alpha.resolve()),
        str(bravo.resolve()),
        str(existing.resolve()),
    }
    assert prefs.discovery_roots == [str(workspace.resolve())]


def test_discover_reprompts_after_invalid_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    workspace = tmp_path / "workspace"
    repo = _git_repo(workspace / "api")

    result = CliRunner().invoke(cli, ["discover", str(workspace)], input="9\n1\n")

    assert result.exit_code == 0, result.output
    assert "outside 1-1" in result.output
    assert (repo / "spec.yaml").is_file()


def test_discover_reports_when_every_repo_is_initialized(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    workspace = tmp_path / "workspace"
    repo = _git_repo(workspace / "api")
    (repo / "spec.yaml").write_text(
        'schema: "spec/v0.1"\nname: api\ncloud:\n  project: api\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["discover", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "Every discovered Git repository already has Spec and is registered" in result.output
    assert load_preferences().bundles == [str(repo.resolve())]


def test_rootless_discover_uses_saved_workspace_outside_current_directory(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    workspace = tmp_path / "workspace"
    repo = _git_repo(workspace / "api")

    first = CliRunner().invoke(cli, ["discover", str(workspace), "--all"])
    assert first.exit_code == 0, first.output
    monkeypatch.chdir(tmp_path)

    second = CliRunner().invoke(cli, ["discover", "--dry-run"])

    assert second.exit_code == 0, second.output
    assert "Scanning this machine under" in second.output
    assert "api · Spec initialized" in second.output
    assert (repo / "spec.yaml").is_file()


def test_discover_all_pushes_each_newly_initialized_repository(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    workspace = tmp_path / "workspace"
    alpha = _git_repo(workspace / "alpha")
    bravo = _git_repo(workspace / "bravo")
    pushed = []

    def fake_push(root, *, dry_run, no_review, reviewers):
        pushed.append((root, dry_run, no_review, reviewers))
        return 0

    monkeypatch.setattr(
        "spec_cli.commands.discover.run_push_for_bundle",
        fake_push,
    )
    result = CliRunner().invoke(
        cli,
        ["discover", str(workspace), "--all", "--push"],
    )

    assert result.exit_code == 0, result.output
    assert [row[0] for row in pushed] == [alpha.resolve(), bravo.resolve()]
    assert all(row[1:] == (False, False, ()) for row in pushed)
    assert "Pushed all 2 newly initialized repositories" in result.output


def test_discover_push_failure_does_not_skip_later_repository(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    workspace = tmp_path / "workspace"
    alpha = _git_repo(workspace / "alpha")
    bravo = _git_repo(workspace / "bravo")
    pushed = []

    def fake_push(root, **kwargs):
        pushed.append(root)
        return 1 if root == alpha else 0

    monkeypatch.setattr(
        "spec_cli.commands.discover.run_push_for_bundle",
        fake_push,
    )
    result = CliRunner().invoke(
        cli,
        ["discover", str(workspace), "--all", "--push"],
    )

    assert result.exit_code == 1
    assert pushed == [alpha.resolve(), bravo.resolve()]
    assert "Initialized but could not push" in result.output
