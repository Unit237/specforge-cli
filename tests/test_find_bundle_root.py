"""``find_bundle_root`` — walk-up, git-tracked, and descendant discovery."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from spec_cli.config import BundleNotFoundError, find_bundle_root


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


def test_find_bundle_root_nested_cwd_git_tracked(tmp_path: Path) -> None:
    repo = tmp_path / "mono"
    repo.mkdir()
    _git(repo, "init")
    spec_dir = repo / "spec"
    spec_dir.mkdir()
    manifest = spec_dir / "spec.yaml"
    manifest.write_text(
        yaml.safe_dump({"schema": "spec/v0.1", "name": "nested"}),
        encoding="utf-8",
    )
    _git(repo, "add", "spec/spec.yaml")
    _git(repo, "commit", "-m", "init")
    nested = repo / "apps" / "foo"
    nested.mkdir(parents=True)
    os.chdir(nested)
    assert find_bundle_root().resolve() == spec_dir.resolve()


def test_find_bundle_root_spec_bundle_root_override(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    a = repo / "a"
    b = repo / "b"
    for d in (a, b):
        d.mkdir(parents=True)
        (d / "spec.yaml").write_text(
            yaml.safe_dump({"schema": "spec/v0.1", "name": d.name}),
            encoding="utf-8",
        )
    os.chdir(b)
    os.environ["SPEC_BUNDLE_ROOT"] = str(a)
    try:
        assert find_bundle_root().resolve() == a.resolve()
    finally:
        del os.environ["SPEC_BUNDLE_ROOT"]


def test_find_bundle_root_multiple_bundles_no_spec_pref_raises(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "r2"
    repo.mkdir()
    _git(repo, "init")
    for name in ("alpha", "beta"):
        d = repo / name
        d.mkdir()
        (d / "spec.yaml").write_text(
            yaml.safe_dump({"schema": "spec/v0.1", "name": name}),
            encoding="utf-8",
        )
        _git(repo, "add", f"{name}/spec.yaml")
    _git(repo, "commit", "-m", "init")
    os.chdir(repo)
    with pytest.raises(BundleNotFoundError, match="SPEC_BUNDLE_ROOT"):
        find_bundle_root()


def test_find_bundle_root_descendant_wrapper_layout(tmp_path: Path) -> None:
    """Parent cwd with bundle in ``<parent>/spec/`` (outside any git repo)."""
    wrapper = tmp_path / "lightreach-io" / "spec"
    bundle = wrapper / "spec"
    bundle.mkdir(parents=True)
    (bundle / "spec.yaml").write_text(
        yaml.safe_dump({"schema": "spec/v0.1", "name": "nested"}),
        encoding="utf-8",
    )
    os.chdir(wrapper)
    assert find_bundle_root().resolve() == bundle.resolve()


def test_find_bundle_root_descendant_prefers_spec_child(tmp_path: Path) -> None:
    wrapper = tmp_path / "mono"
    wrapper.mkdir()
    a = wrapper / "spec"
    b = wrapper / "other" / "bundle"
    for d in (a, b):
        d.mkdir(parents=True)
        (d / "spec.yaml").write_text(
            yaml.safe_dump({"schema": "spec/v0.1", "name": d.name}),
            encoding="utf-8",
        )
    os.chdir(wrapper)
    assert find_bundle_root().resolve() == a.resolve()


def test_find_bundle_root_descendant_multiple_ambiguous_raises(tmp_path: Path) -> None:
    wrapper = tmp_path / "mono"
    wrapper.mkdir()
    for name in ("alpha", "beta"):
        d = wrapper / name
        d.mkdir()
        (d / "spec.yaml").write_text(
            yaml.safe_dump({"schema": "spec/v0.1", "name": name}),
            encoding="utf-8",
        )
    os.chdir(wrapper)
    with pytest.raises(BundleNotFoundError, match="SPEC_BUNDLE_ROOT"):
        find_bundle_root()
