"""``spec bundle root`` — cwd resolution for shell autostart fallback."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


def _run_bundle_root(cwd: Path, *, quiet: bool = False) -> subprocess.CompletedProcess[str]:
    args = [sys.executable, "-m", "spec_cli", "bundle", "root"]
    if quiet:
        args.append("--quiet")
    return subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_bundle_root_quiet_nested_monorepo_cwd(tmp_path: Path) -> None:
    repo = tmp_path / "mono"
    repo.mkdir()
    _git(repo, "init")
    spec_dir = repo / "spec"
    spec_dir.mkdir()
    (spec_dir / "spec.yaml").write_text(
        yaml.safe_dump({"schema": "spec/v0.1", "name": "nested"}),
        encoding="utf-8",
    )
    _git(repo, "add", "spec/spec.yaml")
    _git(repo, "commit", "-m", "init")
    nested = repo / "apps" / "foo"
    nested.mkdir(parents=True)

    proc = _run_bundle_root(nested, quiet=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(spec_dir.resolve())


def test_bundle_root_quiet_missing_bundle(tmp_path: Path) -> None:
    proc = _run_bundle_root(tmp_path, quiet=True)
    assert proc.returncode == 1
    assert proc.stdout.strip() == ""


def test_bundle_root_quiet_descendant_wrapper_layout(tmp_path: Path) -> None:
    wrapper = tmp_path / "parent"
    bundle = wrapper / "spec"
    bundle.mkdir(parents=True)
    (bundle / "spec.yaml").write_text(
        yaml.safe_dump({"schema": "spec/v0.1", "name": "nested"}),
        encoding="utf-8",
    )
    proc = _run_bundle_root(wrapper, quiet=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(bundle.resolve())


def test_bundle_root_honors_spec_bundle_root(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        (d / "spec.yaml").write_text(
            yaml.safe_dump({"schema": "spec/v0.1", "name": d.name}),
            encoding="utf-8",
        )
    env = {**os.environ, "SPEC_BUNDLE_ROOT": str(a)}
    proc = subprocess.run(
        [sys.executable, "-m", "spec_cli", "bundle", "root", "--quiet"],
        cwd=str(b),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(a.resolve())
