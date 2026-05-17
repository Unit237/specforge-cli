"""``path_intersects_bundle`` — cwd ↔ bundle scoping."""

from __future__ import annotations

from pathlib import Path

from spec_cli.bundle_scope import path_intersects_bundle


def test_path_intersects_bundle_exact_and_inside(tmp_path: Path) -> None:
    bundle = tmp_path / "services" / "billing"
    bundle.mkdir(parents=True)
    inside = bundle / "src"
    inside.mkdir()
    assert path_intersects_bundle(bundle, bundle)
    assert path_intersects_bundle(inside, bundle)


def test_path_intersects_bundle_ancestor_parent_folder(tmp_path: Path) -> None:
    parent = tmp_path / "wrapper"
    bundle = parent / "spec"
    bundle.mkdir(parents=True)
    sibling = tmp_path / "other"
    sibling.mkdir()
    assert path_intersects_bundle(parent, bundle)
    assert not path_intersects_bundle(sibling, bundle)


def test_path_intersects_bundle_unrelated_path(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    assert not path_intersects_bundle(other, bundle)
