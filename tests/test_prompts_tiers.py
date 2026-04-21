"""Tests for the two-tier prompts layout and tier-aware compile assembly.

Covers:
- Tier classification from a bundle-relative path
- Directory iteration per tier (captured/curated/pending/legacy)
- Compile assembly excludes `_pending/` and splits curated vs captured
- The compile prompt renders two distinct sections with the expected headings
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spec_cli.compile_assembly import assemble_bundle, render_compile_prompt
from spec_cli.config import load_manifest
from spec_cli.prompts.tiers import (
    Tier,
    classify_tier,
    count_tiers,
    iter_captured,
    iter_compilable,
    iter_curated,
    iter_legacy,
    iter_pending,
)


PROMPT_BODY_TEMPLATE = (
    'schema = "spec.prompts/v0.1"\n'
    "[commit]\n"
    'branch = "main"\n'
    'author_name = "Test"\n'
    'author_email = "test@example.com"\n'
    "\n"
    "[[sessions]]\n"
    'id = "{sid}"\n'
    'source = "manual"\n'
    'title = "{title}"\n'
    "\n"
    "  [[sessions.turns]]\n"
    '  role = "user"\n'
    '  text = "{text}"\n'
)


def _write_prompt(path: Path, *, sid: str, title: str, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        PROMPT_BODY_TEMPLATE.format(sid=sid, title=title, text=text),
        encoding="utf-8",
    )


def _make_bundle(tmp_path: Path) -> Path:
    (tmp_path / "spec.yaml").write_text(
        "schema: spec/v0.1\n"
        "name: tier-demo\n"
        "spec:\n"
        "  entry: docs/product.md\n"
        "  include: [\"docs/**/*.md\"]\n"
        "output:\n"
        "  target: ./out\n",
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "product.md").write_text("# Product\n\nBuild a thing.\n")
    return tmp_path


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel,expected",
    [
        ("prompts/captured/2026-04-21T00-00-00Z.prompts", Tier.CAPTURED),
        ("prompts/curated/2026-04-21T00-00-00Z.prompts", Tier.CURATED),
        ("prompts/curated/_pending/2026-04-21T00-00-00Z.prompts", Tier.PENDING),
        ("prompts/2026-04-21T00-00-00Z.prompts", Tier.LEGACY),
        # Bogus: wrong extension, outside prompts/, nested garbage
        ("prompts/captured/nope.txt", None),
        ("other/captured/x.prompts", None),
        ("prompts/", None),
        ("prompts/curated/nested/x.prompts", Tier.CURATED),  # nested curated still curated
    ],
)
def test_classify_tier(rel: str, expected: Tier | None) -> None:
    assert classify_tier(rel) == expected


def test_classify_tier_pending_beats_curated() -> None:
    # `_pending/` is a subdirectory of `curated/`, so the ordering in the
    # classifier matters: pending must win, otherwise a pending file would
    # leak into the compiler.
    rel = "prompts/curated/_pending/x.prompts"
    assert classify_tier(rel) is Tier.PENDING


# ---------------------------------------------------------------------------
# Tier iteration
# ---------------------------------------------------------------------------


def test_iter_tiers_round_trip(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)

    _write_prompt(root / "prompts" / "captured" / "a.prompts", sid="a", title="A", text="a")
    _write_prompt(root / "prompts" / "curated" / "b.prompts", sid="b", title="B", text="b")
    _write_prompt(
        root / "prompts" / "curated" / "_pending" / "c.prompts",
        sid="c",
        title="C",
        text="c",
    )
    _write_prompt(root / "prompts" / "d.prompts", sid="d", title="D", text="d")

    assert [tp.rel for tp in iter_captured(root)] == ["prompts/captured/a.prompts"]
    assert [tp.rel for tp in iter_curated(root)] == ["prompts/curated/b.prompts"]
    assert [tp.rel for tp in iter_pending(root)] == [
        "prompts/curated/_pending/c.prompts"
    ]
    assert [tp.rel for tp in iter_legacy(root)] == ["prompts/d.prompts"]

    counts = count_tiers(root)
    assert (counts.captured, counts.curated, counts.pending, counts.legacy) == (1, 1, 1, 1)


def test_iter_compilable_excludes_pending(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    _write_prompt(root / "prompts" / "captured" / "a.prompts", sid="a", title="A", text="a")
    _write_prompt(root / "prompts" / "curated" / "b.prompts", sid="b", title="B", text="b")
    _write_prompt(
        root / "prompts" / "curated" / "_pending" / "c.prompts",
        sid="c",
        title="C",
        text="c",
    )

    rels = [tp.rel for tp in iter_compilable(root)]
    assert "prompts/curated/_pending/c.prompts" not in rels
    # Order is curated → legacy → captured by contract.
    assert rels == [
        "prompts/curated/b.prompts",
        "prompts/captured/a.prompts",
    ]


# ---------------------------------------------------------------------------
# Compile assembly
# ---------------------------------------------------------------------------


def test_assemble_bundle_splits_curated_and_captured(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    _write_prompt(
        root / "prompts" / "captured" / "cap.prompts",
        sid="cap",
        title="cap",
        text="captured text",
    )
    _write_prompt(
        root / "prompts" / "curated" / "cur.prompts",
        sid="cur",
        title="cur",
        text="curated text",
    )
    _write_prompt(
        root / "prompts" / "curated" / "_pending" / "pend.prompts",
        sid="pend",
        title="pend",
        text="pending text",
    )

    manifest = load_manifest(root)
    bundle = assemble_bundle(root, manifest)

    curated_rels = [rel for rel, _ in bundle.curated_prompts]
    captured_rels = [rel for rel, _ in bundle.captured_prompts]
    flat_rels = [rel for rel, _ in bundle.prompts_files]

    assert curated_rels == ["prompts/curated/cur.prompts"]
    assert captured_rels == ["prompts/captured/cap.prompts"]
    # Flat list preserves disjointness and excludes pending.
    assert "prompts/curated/_pending/pend.prompts" not in flat_rels
    assert set(flat_rels) == set(curated_rels) | set(captured_rels)


def test_render_compile_prompt_labels_tiers(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    _write_prompt(
        root / "prompts" / "captured" / "cap.prompts",
        sid="cap",
        title="cap-title",
        text="captured text",
    )
    _write_prompt(
        root / "prompts" / "curated" / "cur.prompts",
        sid="cur",
        title="cur-title",
        text="curated text",
    )
    _write_prompt(
        root / "prompts" / "curated" / "_pending" / "pend.prompts",
        sid="pend",
        title="pend-title",
        text="pending text",
    )

    manifest = load_manifest(root)
    bundle = assemble_bundle(root, manifest)
    text = render_compile_prompt(bundle)

    assert "## Curated prompt history" in text
    assert "## Captured prompt history (advisory)" in text
    # Curated content is present; pending content is not.
    assert "curated text" in text
    assert "captured text" in text
    assert "pending text" not in text
    # Curated section appears before captured in the rendered prompt.
    assert text.index("## Curated prompt history") < text.index(
        "## Captured prompt history"
    )


def test_legacy_root_prompts_render_as_curated(tmp_path: Path) -> None:
    # Files at the legacy `prompts/` root (no subdir) are grandfathered as
    # curated. This test guards the backwards-compat path so existing bundles
    # don't silently start showing up under "Captured".
    root = _make_bundle(tmp_path)
    _write_prompt(
        root / "prompts" / "legacy.prompts",
        sid="legacy",
        title="legacy-title",
        text="legacy body text",
    )

    manifest = load_manifest(root)
    bundle = assemble_bundle(root, manifest)
    text = render_compile_prompt(bundle)

    assert [rel for rel, _ in bundle.curated_prompts] == ["prompts/legacy.prompts"]
    assert bundle.captured_prompts == []
    assert "legacy body text" in text
    # No captured section should be emitted when only legacy files exist.
    assert "## Captured prompt history" not in text
