"""Batch behavior for ``spec push --all``."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from spec_cli.cli import cli
from spec_cli.commands.push import collect_push_all_roots, run_push_for_bundle
from spec_cli.preferences import remember_bundle


push_module = importlib.import_module("spec_cli.commands.push")


def _bundle(path: Path, name: str) -> Path:
    path.mkdir(parents=True)
    (path / "spec.yaml").write_text(
        f'schema: "spec/v0.1"\nname: {name}\ncloud:\n  project: {name}\n',
        encoding="utf-8",
    )
    return path


def test_collect_push_all_roots_combines_registry_and_cwd_scan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    registered = _bundle(tmp_path / "elsewhere" / "registered", "registered")
    discovered = _bundle(tmp_path / "workspace" / "discovered", "discovered")
    missing = tmp_path / "deleted"
    remember_bundle(registered)
    prefs = remember_bundle(missing)
    assert str(missing.resolve()) in prefs.bundles

    roots = collect_push_all_roots(tmp_path / "workspace")

    assert roots == sorted(
        [registered.resolve(), discovered.resolve()],
        key=lambda root: str(root).lower(),
    )


def test_push_all_rejects_single_bundle_target_options() -> None:
    runner = CliRunner()

    url = runner.invoke(cli, ["push", "https://example.com/team/repo", "--all"])
    project = runner.invoke(cli, ["push", "--all", "--project", "team/repo"])

    assert url.exit_code == 1
    assert "cannot be combined" in url.output
    assert project.exit_code == 1
    assert "cannot be combined" in project.output


def test_push_all_continues_after_failure_and_forwards_options(
    tmp_path: Path,
    monkeypatch,
) -> None:
    roots = [_bundle(tmp_path / name, name) for name in ("alpha", "bravo", "charlie")]
    calls: list[tuple[Path, bool, bool, tuple[str, ...]]] = []

    monkeypatch.setattr(push_module, "collect_push_all_roots", lambda: roots)

    def fake_run(root, *, dry_run, no_review, reviewers):
        calls.append((root, dry_run, no_review, reviewers))
        return 1 if root.name == "bravo" else 0

    monkeypatch.setattr(push_module, "run_push_for_bundle", fake_run)
    result = CliRunner().invoke(
        cli,
        [
            "push",
            "--all",
            "--dry-run",
            "--no-review",
            "--reviewer",
            "one@example.com",
            "--reviewer",
            "two@example.com",
        ],
    )

    assert result.exit_code == 1
    assert [call[0] for call in calls] == roots
    assert all(call[1] is True and call[2] is True for call in calls)
    assert all(call[3] == ("one@example.com", "two@example.com") for call in calls)
    assert "failed for 1 of 3 bundles" in result.output
    assert "bravo" in result.output


def test_push_all_succeeds_when_every_bundle_processes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    roots = [_bundle(tmp_path / name, name) for name in ("alpha", "bravo")]
    monkeypatch.setattr(push_module, "collect_push_all_roots", lambda: roots)
    monkeypatch.setattr(push_module, "run_push_for_bundle", lambda *args, **kwargs: 0)

    result = CliRunner().invoke(cli, ["push", "--all"])

    assert result.exit_code == 0, result.output
    assert "Processed all 2 registered Spec bundles" in result.output


def test_run_push_for_bundle_uses_explicit_bundle_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _bundle(tmp_path / "bundle", "bundle")
    captured: dict = {}

    def fake_run(args, *, cwd, env, check):
        captured.update(args=args, cwd=cwd, env=env, check=check)
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(push_module.subprocess, "run", fake_run)

    code = run_push_for_bundle(
        root,
        dry_run=True,
        no_review=True,
        reviewers=("reviewer@example.com",),
    )

    assert code == 7
    assert captured["args"][-4:] == [
        "--dry-run",
        "--no-review",
        "--reviewer",
        "reviewer@example.com",
    ]
    assert captured["cwd"] == root
    assert captured["env"]["SPEC_BUNDLE_ROOT"] == str(root.resolve())
    assert captured["check"] is False
