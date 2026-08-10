from __future__ import annotations

from click.testing import CliRunner

from spec_cli.cli import cli
from spec_cli.commands.init import (
    AGENTS_COORDINATION_BLOCK_BEGIN,
    AGENTS_COORDINATION_BLOCK_END,
)


def _bundle(tmp_path):
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "spec.yaml").write_text('schema: "spec/v0.1"\nname: Demo\n', encoding="utf-8")
    return root


def test_upgrade_rules_appends_agents_coordination_without_overwriting(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    root = _bundle(tmp_path)
    agents = root / "AGENTS.md"
    agents.write_text("# Team instructions\n\nKeep this text.\n", encoding="utf-8")
    monkeypatch.chdir(root)
    runner = CliRunner()

    first = runner.invoke(cli, ["init", "--upgrade-rules"], catch_exceptions=False)
    second = runner.invoke(cli, ["init", "--upgrade-rules"], catch_exceptions=False)

    assert first.exit_code == 0
    assert second.exit_code == 0
    body = agents.read_text(encoding="utf-8")
    assert "Keep this text." in body
    assert body.count(AGENTS_COORDINATION_BLOCK_BEGIN) == 1
    assert body.count(AGENTS_COORDINATION_BLOCK_END) == 1
    assert "Read `.spec/team-coordination.md`" in body
    assert "spec locks check <bundle-relative-path>" in body
    assert "Never hand-edit files under `.spec/`" in body


def test_upgrade_rules_creates_agents_coordination_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    root = _bundle(tmp_path)
    monkeypatch.chdir(root)

    result = CliRunner().invoke(cli, ["init", "--upgrade-rules"], catch_exceptions=False)

    assert result.exit_code == 0
    body = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert body.startswith(AGENTS_COORDINATION_BLOCK_BEGIN)
    assert body.rstrip().endswith(AGENTS_COORDINATION_BLOCK_END)


def test_initial_init_appends_coordination_to_existing_agents_without_overwriting(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    root = tmp_path / "repo"
    root.mkdir()
    agents = root / "AGENTS.md"
    agents.write_text("# Team instructions\n\nKeep this text.\n", encoding="utf-8")
    monkeypatch.chdir(root)

    result = CliRunner().invoke(cli, ["init"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    body = agents.read_text(encoding="utf-8")
    assert "Keep this text." in body
    assert body.count(AGENTS_COORDINATION_BLOCK_BEGIN) == 1
    assert "appended Spec coordination block" in result.output
