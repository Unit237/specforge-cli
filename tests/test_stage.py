import shutil
from pathlib import Path

import pytest

from spec_cli.stage import (
    InvalidBundleError,
    assert_push_invariants,
    classify_working_tree,
    ensure_root_manifest_staged,
    historical_bundle_paths,
    load_index,
    record_bundle_path,
    save_index,
    sha256,
)


def _make_bundle(tmp_path: Path) -> Path:
    (tmp_path / "spec.yaml").write_text("schema: spec/v0.1\nname: demo\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "product.md").write_text("# Product\n")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "2026-04-21T11-47-35Z.prompts").write_text(
        'schema = "spec.prompts/v0.1"\n[commit]\nbranch = "main"\n'
        'author_name = "t"\nauthor_email = "t@example.com"\n'
        '[[sessions]]\nid = "x"\nsource = "manual"\n'
        '[[sessions.turns]]\nrole = "user"\ntext = "hi"\n'
    )
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return tmp_path


def test_classify_working_tree_separates_ignored(tmp_path):
    root = _make_bundle(tmp_path)
    idx = load_index(root)
    lines = classify_working_tree(root, idx)
    by_state: dict[str, list[str]] = {}
    for ln in lines:
        by_state.setdefault(ln.state, []).append(ln.rel)

    assert "logo.png" in by_state["ignored"]
    assert "docs/product.md" in by_state["untracked"]
    assert "spec.yaml" in by_state["untracked"]
    # The `.prompts` file is a first-class spec file, not ignored.
    assert any(
        ln.rel == "prompts/2026-04-21T11-47-35Z.prompts"
        and ln.kind == "prompts"
        and ln.state == "untracked"
        for ln in lines
    )


def test_push_invariants_rejects_empty():
    with pytest.raises(InvalidBundleError):
        assert_push_invariants(Path("/tmp"), {})


def test_push_invariants_requires_md():
    with pytest.raises(InvalidBundleError):
        assert_push_invariants(Path("/tmp"), {"spec.yaml": "x"})


def test_push_invariants_ok():
    assert_push_invariants(
        Path("/tmp"),
        {"spec.yaml": "x", "docs/product.md": "y"},
    )


def test_ensure_root_manifest_staged_after_successful_push_semantics(tmp_path: Path) -> None:
    """Mirror real flow: push clears manifest from staged; next hook-only commit must still push."""
    root = _make_bundle(tmp_path)
    idx = load_index(root)
    idx.staged["docs/product.md"] = sha256((root / "docs" / "product.md").read_bytes())
    idx.pushed["spec.yaml"] = sha256((root / "spec.yaml").read_bytes())
    save_index(idx)

    ensure_root_manifest_staged(idx)

    assert "spec.yaml" in idx.staged
    assert idx.staged["spec.yaml"] == sha256((root / "spec.yaml").read_bytes())
    assert_push_invariants(root, idx.staged)


def test_ensure_root_manifest_staged_noop_when_already_present(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    idx = load_index(root)
    h = sha256((root / "spec.yaml").read_bytes())
    idx.staged["spec.yaml"] = h
    idx.staged["docs/product.md"] = sha256((root / "docs" / "product.md").read_bytes())
    save_index(idx)
    ensure_root_manifest_staged(idx)
    assert idx.staged["spec.yaml"] == h


def test_push_invariants_rejects_md_under_prompts():
    # `.md` under `prompts/` is a classic copy-paste mistake. Catch it loudly.
    with pytest.raises(InvalidBundleError) as exc:
        assert_push_invariants(
            Path("/tmp"),
            {
                "spec.yaml": "x",
                "docs/product.md": "y",
                "prompts/scaffold.md": "z",
            },
        )
    assert "prompts/" in str(exc.value)


def test_push_invariants_rejects_nested_spec_yaml():
    # Defense in depth: even if a stale index entry from before the
    # `is_spec_file` tightening (or a hand-edited `.spec/index.json`)
    # gets a nested `spec.yaml` into the staged set, the push-time
    # invariant catches it with a clear, actionable error rather than
    # letting the server return its less-specific rejection.
    with pytest.raises(InvalidBundleError) as exc:
        assert_push_invariants(
            Path("/tmp"),
            {
                "spec.yaml": "x",
                "docs/product.md": "y",
                "backend/app/spec.yaml": "z",
            },
        )
    msg = str(exc.value)
    assert "backend/app/spec.yaml" in msg
    assert "bundle root" in msg
    assert "spec unstage" in msg


def test_sha256_stable():
    assert sha256(b"") == sha256(b"")
    assert sha256(b"hi") != sha256(b"ho")


# ---------------------------------------------------------------------------
# Bundle-path aliases (Fix #2): index remembers every location the bundle
# has lived at so a folder rename doesn't orphan its prompt history.
# ---------------------------------------------------------------------------


def test_record_bundle_path_is_idempotent(tmp_path):
    root = _make_bundle(tmp_path)
    record_bundle_path(root)
    record_bundle_path(root)
    record_bundle_path(root)
    idx = load_index(root)
    # Same path recorded three times still appears exactly once.
    assert idx.bundle_paths == [str(root.resolve())]


def test_historical_bundle_paths_includes_current_first(tmp_path):
    root = _make_bundle(tmp_path)
    record_bundle_path(root)
    paths = historical_bundle_paths(root)
    assert paths[0] == root.resolve()


def test_historical_bundle_paths_survives_rename(tmp_path):
    """The whole point of Fix #2: after a folder move, the *old* path
    is still in the historical list because `.spec/index.json` travels
    with the folder."""
    old_root = tmp_path / "billing"
    old_root.mkdir()
    _make_bundle(old_root)
    record_bundle_path(old_root)

    new_root = tmp_path / "payments"
    shutil.move(str(old_root), str(new_root))

    # Simulate the next `prompts capture` from the new location.
    record_bundle_path(new_root)

    paths = historical_bundle_paths(new_root)
    resolved = [p.resolve() for p in paths]
    # Current path comes first; the old path is still in the list,
    # which is what lets the source adapters look in two places.
    assert resolved[0] == new_root.resolve()
    assert old_root.resolve() in resolved


def test_load_index_tolerates_missing_bundle_paths_field(tmp_path):
    """Old indexes (pre-Fix #2) didn't carry `bundle_paths`. Loading
    one must not crash; the field defaults to empty."""
    root = _make_bundle(tmp_path)
    spec_dir = root / ".spec"
    spec_dir.mkdir(exist_ok=True)
    # Hand-write an old-shape index.json — only `staged` and `pushed`.
    (spec_dir / "index.json").write_text(
        '{"staged": {}, "pushed": {}}', encoding="utf-8"
    )
    idx = load_index(root)
    assert idx.bundle_paths == []
