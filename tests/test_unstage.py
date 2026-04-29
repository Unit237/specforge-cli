from pathlib import Path

from click.testing import CliRunner

from spec_cli.commands.unstage import _rels_to_unstage
from spec_cli.stage import load_index, save_index, sha256
from spec_cli.commands.unstage import unstage_cmd


def _make_minimal_bundle(tmp: Path) -> Path:
    (tmp / "spec.yaml").write_text("schema: spec/v0.1\nname: t\n", encoding="utf-8")
    (tmp / "docs").mkdir()
    (tmp / "docs" / "a.md").write_text("# a\n", encoding="utf-8")
    (tmp / "prompts").mkdir()
    f_bad = tmp / "prompts" / "x.md"
    f_bad.write_text("# oops\n", encoding="utf-8")
    f_ok = tmp / "prompts" / "b.prompts"
    f_ok.write_text('schema = "spec.prompts/v0.1"\n[commit]\n', encoding="utf-8")
    idx = load_index(tmp)
    for rel, p in [
        ("spec.yaml", tmp / "spec.yaml"),
        ("docs/a.md", tmp / "docs" / "a.md"),
        ("prompts/x.md", f_bad),
        ("prompts/b.prompts", f_ok),
    ]:
        idx.staged[rel] = sha256(p.read_bytes())
    save_index(idx)
    return tmp


def test_rels_to_unstage_file_and_prefix(tmp_path, monkeypatch):
    root = _make_minimal_bundle(tmp_path)
    monkeypatch.chdir(root)
    staged = load_index(root).staged
    r = _rels_to_unstage(root, "prompts/x.md", staged)
    assert r == {"prompts/x.md"}
    r2 = _rels_to_unstage(root, "prompts", staged)
    assert r2 == {"prompts/x.md", "prompts/b.prompts"}


def test_unstage_cli_drops_path(tmp_path, monkeypatch):
    root = _make_minimal_bundle(tmp_path)
    monkeypatch.chdir(root)
    runner = CliRunner()
    result = runner.invoke(
        unstage_cmd,
        ["prompts/x.md"],
    )
    assert result.exit_code == 0, result.output
    idx = load_index(root)
    assert "prompts/x.md" not in idx.staged
    assert "spec.yaml" in idx.staged
