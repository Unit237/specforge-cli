"""Tests for `spec git-hooks` and bundle resolution for nested repos."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
import yaml

from spec_cli.commands.git_hooks import (
    discover_bundle_roots_under_git_root,
    resolve_bundle_root_for_git_hook,
    run_git_hook_pre_commit,
    run_git_hook_pre_push,
)
from spec_cli.commands.init import (
    PRE_COMMIT_HOOK_BEGIN,
    PRE_COMMIT_HOOK_END,
    PRE_COMMIT_HOOK_BODY,
    _uninstall_git_hook_segment,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


def test_resolve_bundle_root_prefers_spec_subdir_when_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "spec").mkdir()
    (repo / "other").mkdir()
    for rel in ("spec/spec.yaml", "other/spec.yaml"):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump({"schema": "spec/v0.1", "name": "x"}), encoding="utf-8")
        _git(repo, "add", rel)
    _git(repo, "commit", "-m", "init")

    monkeypatch.chdir(repo)
    chosen = resolve_bundle_root_for_git_hook(repo)
    assert chosen == (repo / "spec").resolve()


def test_discover_bundle_roots_sorts_and_dedupes(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init")
    (repo / "a").mkdir()
    (repo / "b").mkdir()
    for name in ("a/spec.yaml", "b/spec.yaml"):
        p = repo / name
        p.write_text(yaml.safe_dump({"schema": "spec/v0.1"}), encoding="utf-8")
        _git(repo, "add", name)
    roots = discover_bundle_roots_under_git_root(repo)
    assert roots == [(repo / "a").resolve(), (repo / "b").resolve()]


def test_run_pre_commit_invokes_spec_add_for_bundle_paths(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    bundle = repo / "bundle"
    (bundle / "docs").mkdir(parents=True)
    manifest = bundle / "spec.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema": "spec/v0.1",
                "name": "t",
                "spec": {"entry": "docs/x.md", "include": ["docs/**/*.md"], "exclude": []},
            }
        ),
        encoding="utf-8",
    )
    doc = bundle / "docs" / "x.md"
    doc.write_text("# hi\n", encoding="utf-8")
    extra = repo / "README.md"
    extra.write_text("n\n", encoding="utf-8")
    (bundle / "README.md").write_text("# aux\n", encoding="utf-8")
    _git(
        repo,
        "add",
        "bundle/spec.yaml",
        "bundle/docs/x.md",
        "README.md",
        "bundle/README.md",
    )
    monkeypatch.chdir(repo)
    monkeypatch.setenv("SPEC_BUNDLE_ROOT", str(bundle))

    recorded: list[tuple[list[str], str]] = []
    real_run = subprocess.run

    def fake_run(cmd, cwd=None, **kwargs):  # type: ignore[no-untyped-def]
        if cmd and cmd[0] == "spec":
            recorded.append((list(cmd), str(cwd or "")))
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, cwd=cwd, **kwargs)

    monkeypatch.setattr("spec_cli.commands.git_hooks.subprocess.run", fake_run)
    monkeypatch.setattr(
        "spec_cli.commands.git_hooks._spec_cmd_prefix",
        lambda: ["spec"],
    )

    run_git_hook_pre_commit()

    adds = [r for r in recorded if "add" in r[0]]
    assert adds
    flat = " ".join(" ".join(a[0]) for a in adds)
    assert "spec.yaml" in flat and "docs/x.md" in flat
    assert "README.md" not in flat
    assert all(Path(cwd) == bundle.resolve() for _, cwd in adds)


def test_run_pre_commit_rename_unstages_old_and_adds_new(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    bundle = repo / "bundle"
    (bundle / "docs").mkdir(parents=True)
    (bundle / "spec.yaml").write_text(
        yaml.safe_dump(
            {
                "schema": "spec/v0.1",
                "name": "t",
                "spec": {"entry": "docs/a.md", "include": ["docs/**/*.md"], "exclude": []},
            }
        ),
        encoding="utf-8",
    )
    (bundle / "docs" / "a.md").write_text("# a\n", encoding="utf-8")
    _git(repo, "add", "bundle")
    _git(repo, "commit", "-m", "init")
    _git(repo, "mv", "bundle/docs/a.md", "bundle/docs/b.md")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("SPEC_BUNDLE_ROOT", str(bundle))

    recorded: list[tuple[list[str], str]] = []
    real_run = subprocess.run

    def fake_run(cmd, cwd=None, **kwargs):  # type: ignore[no-untyped-def]
        if cmd and cmd[0] == "spec":
            recorded.append((list(cmd), str(cwd or "")))
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, cwd=cwd, **kwargs)

    monkeypatch.setattr("spec_cli.commands.git_hooks.subprocess.run", fake_run)
    monkeypatch.setattr(
        "spec_cli.commands.git_hooks._spec_cmd_prefix",
        lambda: ["spec"],
    )

    run_git_hook_pre_commit()

    cmds = [" ".join(x[0]) for x in recorded]
    assert any("unstage" in c and "docs/a.md" in c for c in cmds)
    assert any("add" in c and "docs/b.md" in c for c in cmds)


def test_run_pre_push_skips_when_skip_env(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    bundle = repo / "bundle"
    bundle.mkdir()
    (bundle / "spec.yaml").write_text(
        yaml.safe_dump(
            {
                "schema": "spec/v0.1",
                "name": "t",
                "spec": {"entry": "docs/x.md", "include": ["docs/**/*.md"], "exclude": []},
            }
        ),
        encoding="utf-8",
    )
    (bundle / "docs").mkdir()
    (bundle / "docs" / "x.md").write_text("# x\n", encoding="utf-8")
    _git(repo, "add", "bundle")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("SPEC_BUNDLE_ROOT", str(bundle))
    monkeypatch.setenv("SKIP_SPEC_PUSH", "1")

    monkeypatch.setattr("sys.stdin", io.StringIO("refs/heads/main abc refs/heads/main def\n"))
    assert run_git_hook_pre_push() == 0


def test_run_pre_push_skips_tag_only_push(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    bundle = repo / "bundle"
    bundle.mkdir()
    (bundle / "spec.yaml").write_text(
        yaml.safe_dump({"schema": "spec/v0.1", "name": "t"}), encoding="utf-8"
    )
    (bundle / "docs").mkdir()
    (bundle / "docs" / "x.md").write_text("# x\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("SPEC_BUNDLE_ROOT", str(bundle))
    monkeypatch.delenv("SKIP_SPEC_PUSH", raising=False)

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("refs/tags/v1 deadbeef refs/tags/v1 deadbeef\n"),
    )
    assert run_git_hook_pre_push() == 0


def test_uninstall_removes_spec_only_hook_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    git_dir = repo / ".git"
    hook = git_dir / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\n\n" + PRE_COMMIT_HOOK_BODY, encoding="utf-8")
    st, pth = _uninstall_git_hook_segment(
        git_dir, "pre-commit", PRE_COMMIT_HOOK_BEGIN, PRE_COMMIT_HOOK_END
    )
    assert st == "removed"
    assert pth == hook
    assert not hook.exists()


def test_uninstall_strips_block_keeps_user_content(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    git_dir = repo / ".git"
    hook = git_dir / "hooks" / "pre-commit"
    user = "#!/bin/sh\n\necho user-hook\n"
    hook.write_text(user + "\n" + PRE_COMMIT_HOOK_BODY, encoding="utf-8")
    st, _ = _uninstall_git_hook_segment(
        git_dir, "pre-commit", PRE_COMMIT_HOOK_BEGIN, PRE_COMMIT_HOOK_END
    )
    assert st == "stripped"
    left = hook.read_text(encoding="utf-8")
    assert "user-hook" in left
    assert PRE_COMMIT_HOOK_BEGIN not in left


def test_uninstall_missing_hook(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    git_dir = repo / ".git"
    st, pth = _uninstall_git_hook_segment(
        git_dir, "pre-commit", PRE_COMMIT_HOOK_BEGIN, PRE_COMMIT_HOOK_END
    )
    assert st == "missing"
    assert pth == git_dir / "hooks" / "pre-commit"


def test_uninstall_no_markers(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    git_dir = repo / ".git"
    hook = git_dir / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho other\n", encoding="utf-8")
    st, _ = _uninstall_git_hook_segment(
        git_dir, "pre-commit", PRE_COMMIT_HOOK_BEGIN, PRE_COMMIT_HOOK_END
    )
    assert st == "no_spec_block"
    assert hook.read_text(encoding="utf-8") == "#!/bin/sh\necho other\n"
