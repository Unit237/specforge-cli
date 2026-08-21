"""Autorepair for known ``spec.yaml`` corruptions (e.g. ``output:hropic``)."""

from __future__ import annotations

import pytest

from spec_cli.config import (
    ManifestYamlError,
    load_manifest,
)


def _broken_output_hropic() -> str:
    return """schema: spec/v0.1
name: demo
spec:
  entry: docs/product.md
  include: []
  exclude: []
compiler:
  engine: ant
  max_output_tokens: 8000
output:hropic
  model: claude-sonnet-4-5
  temperature: 0.2
  target: ./out
  changelog: true
  commit_style: conventional
cloud:
  project: demo
"""


def test_load_manifest_autorepairs_output_hropic(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SPEC_NO_MANIFEST_AUTOWRITE", raising=False)
    root = tmp_path / "bundle"
    root.mkdir()
    spec = root / "spec.yaml"
    spec.write_text(_broken_output_hropic(), encoding="utf-8")

    m = load_manifest(root)
    assert m.data["compiler"]["model"] == "claude-sonnet-4-5"
    assert m.data["compiler"]["temperature"] == 0.2
    assert m.data["output"]["target"] == "./out"
    assert "output:hropic" not in spec.read_text(encoding="utf-8")
    bak = root / ".spec" / "spec.yaml.invalid-backup"
    assert bak.is_file()
    assert "output:hropic" in bak.read_text(encoding="utf-8")


def test_load_manifest_autorepair_respects_no_disk_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SPEC_NO_MANIFEST_AUTOWRITE", "1")
    root = tmp_path / "bundle"
    root.mkdir()
    spec = root / "spec.yaml"
    original = _broken_output_hropic()
    spec.write_text(original, encoding="utf-8")

    m = load_manifest(root)
    assert m.data["compiler"]["model"] == "claude-sonnet-4-5"
    assert spec.read_text(encoding="utf-8") == original
    assert not (root / ".spec" / "spec.yaml.invalid-backup").is_file()


def test_load_manifest_invalid_yaml_raises_manifest_error(tmp_path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "spec.yaml").write_text(
        "schema: x\nname: y\nthis is not yaml: [\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestYamlError) as ei:
        load_manifest(root)
    assert "Invalid YAML" in str(ei.value)
