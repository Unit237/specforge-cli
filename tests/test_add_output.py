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
