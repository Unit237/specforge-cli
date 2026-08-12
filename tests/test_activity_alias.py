from click.testing import CliRunner

from spec_cli.cli import cli


def test_activity_help_uses_human_facing_command_and_keeps_tool_flag() -> None:
    result = CliRunner().invoke(cli, ["activity", "--help"])

    assert result.exit_code == 0
    assert "Stream every agent turn across your Spec workspace" in result.output
    assert "spec activity --show-tool-runs" in result.output
    assert "--show-tool-runs" in result.output
    assert "Examples:\n    spec team watch" not in result.output
