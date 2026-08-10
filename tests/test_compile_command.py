from __future__ import annotations

from click.testing import CliRunner

from spec_cli.commands.compile import compile_cmd


def test_default_compile_handoff_uses_configured_engine(tmp_path, monkeypatch) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "product.md").write_text("# Product\n\nBuild it.\n")
    (tmp_path / "spec.yaml").write_text(
        """schema: spec/v0.1
name: Example
spec:
  entry: docs/product.md
  include:
    - docs/**/*.md
compiler:
  engine: openai
  model: gpt-5
output:
  target: ./out
"""
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(compile_cmd, [], catch_exceptions=False)

    assert result.exit_code == 0
    assert "ask your coding agent to compile this bundle" in result.output
    assert "configured `openai` engine" in result.output
    assert "OPENAI_API_KEY" in result.output
    assert "Anthropic model" not in result.output
