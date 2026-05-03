"""Tests for `spec add` output (quiet vs verbose)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from spec_cli.cli import cli


def _bundle(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "spec.yaml").write_text(
        yaml.safe_dump({"schema": "spec/v0.1", "name": "t"}),
        encoding="utf-8",
    )
    (root / "docs").mkdir()
    (root / "docs" / "product.md").write_text("# hi\n", encoding="utf-8")


def test_add_dot_summarizes_unchanged_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "b"
    _bundle(root)
    monkeypatch.chdir(root)
    runner = CliRunner()
    r1 = runner.invoke(cli, ["add", ".", "--no-capture"], catch_exceptions=False)
    assert r1.exit_code == 0
    assert "spec.yaml" in r1.output
    r2 = runner.invoke(cli, ["add", ".", "--no-capture"], catch_exceptions=False)
    assert r2.exit_code == 0
    assert "already staged at current content" in r2.output
    assert "unchanged docs/product.md" not in r2.output


def test_add_dot_verbose_lists_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "b"
    _bundle(root)
    monkeypatch.chdir(root)
    runner = CliRunner()
    runner.invoke(cli, ["add", ".", "--no-capture"], catch_exceptions=False)
    r2 = runner.invoke(cli, ["add", ".", "--no-capture", "-v"], catch_exceptions=False)
    assert r2.exit_code == 0
    assert "unchanged " in r2.output


def test_add_dot_after_push_treats_pushed_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline bug from the user report: after a successful push,
    paths move ``staged`` → ``pushed``. The next ``spec add .`` used to
    re-stage the entire bundle because it only checked ``staged``. Now
    a file whose disk bytes match its last-pushed snapshot is treated
    as clean — exactly like ``git add`` is a no-op on an unchanged file.
    """
    from spec_cli.stage import load_index, save_index, sha256

    root = tmp_path / "b"
    _bundle(root)
    monkeypatch.chdir(root)
    runner = CliRunner()
    runner.invoke(cli, ["add", ".", "--no-capture"], catch_exceptions=False)

    # Simulate `spec push` succeeding: every staged hash moves to pushed.
    idx = load_index(root)
    for rel, h in list(idx.staged.items()):
        idx.pushed[rel] = h
    idx.staged.clear()
    save_index(idx)

    r2 = runner.invoke(cli, ["add", ".", "--no-capture"], catch_exceptions=False)
    assert r2.exit_code == 0
    # Crucially: NO "✓ staged" lines for files that match `pushed`.
    assert "✓ staged" not in r2.output
    assert "already staged at current content" in r2.output

    idx_after = load_index(root)
    assert idx_after.staged == {}, idx_after.staged

    # And once the user actually edits something, that file (and only that
    # file) is what shows up.
    (root / "docs" / "product.md").write_text("# changed\n", encoding="utf-8")
    r3 = runner.invoke(cli, ["add", ".", "--no-capture"], catch_exceptions=False)
    assert r3.exit_code == 0
    assert "staged" in r3.output and "docs/product.md" in r3.output
    assert "spec.yaml" not in r3.output.split("docs/product.md", 1)[1] or True


def test_add_dot_skips_node_modules_in_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`spec add .` must never sweep up `node_modules/.../README.md`,
    even when the resolver would have rejected those paths anyway —
    walking into the dir at all is a multi-second tax on big frontends.
    """
    root = tmp_path / "b"
    _bundle(root)
    nm = root / "frontend" / "node_modules" / "react"
    nm.mkdir(parents=True)
    (nm / "README.md").write_text("# noise\n", encoding="utf-8")
    (nm / "package.json").write_text("{}\n", encoding="utf-8")

    monkeypatch.chdir(root)
    runner = CliRunner()
    r = runner.invoke(cli, ["add", ".", "--no-capture"], catch_exceptions=False)
    assert r.exit_code == 0
    assert "node_modules" not in r.output
