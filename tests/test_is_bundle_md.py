"""
Truth-table tests for the bundle-membership resolver.

The resolver is the single source of truth for which `.md` files in a
worktree count as bundle content versus auxiliary docs. It's mirrored
verbatim by `spec-compiler` and the Cloud backend, so this test file
also doubles as the contract those repos pin to. When you change a
case here, change the corresponding case in
`spec-compiler/tests/test_is_bundle_md.py` and
`spec/backend/tests/test_bundle_resolver.py` (when those land).
"""

from __future__ import annotations

import pytest

from spec_cli.constants import (
    AGENT_INSTRUCTION_FILENAMES,
    AGENT_INSTRUCTION_PATTERNS,
    DEFAULT_SPEC_INCLUDE,
    HUMAN_DOC_FILENAMES,
    is_bundle_md,
    is_bundle_path,
)


# ---------------------------------------------------------------------------
# Constants — pin the lists so silent drift is caught at test time.
# ---------------------------------------------------------------------------


def test_agent_allowlist_contents():
    # Filenames are stored lowercased — the resolver matches
    # case-insensitively, so `AGENTS.md` and `agents.md` both hit.
    assert "agents.md" in AGENT_INSTRUCTION_FILENAMES
    assert "claude.md" in AGENT_INSTRUCTION_FILENAMES
    assert "gemini.md" in AGENT_INSTRUCTION_FILENAMES
    assert "llms.txt" in AGENT_INSTRUCTION_FILENAMES
    assert "llms-full.txt" in AGENT_INSTRUCTION_FILENAMES


def test_agent_pattern_list():
    # `.cursor/rules/**/*.mdc` is intentionally absent in v0.2 — `.mdc`
    # isn't on SPEC_EXTENSIONS yet. Tracked as a v0.3 concern; users
    # put rules in AGENTS.md for now.
    assert ".github/copilot-instructions.md" in AGENT_INSTRUCTION_PATTERNS


def test_human_denylist_contents():
    assert "readme.md" in HUMAN_DOC_FILENAMES
    assert "changelog.md" in HUMAN_DOC_FILENAMES
    assert "contributing.md" in HUMAN_DOC_FILENAMES
    assert "license" in HUMAN_DOC_FILENAMES
    assert "license.md" in HUMAN_DOC_FILENAMES


def test_default_include_glob():
    assert DEFAULT_SPEC_INCLUDE == ("docs/**/*.md",)


# ---------------------------------------------------------------------------
# Step 5 — default include glob (`docs/**/*.md`)
# ---------------------------------------------------------------------------


def test_docs_md_is_bundle_by_default():
    assert is_bundle_md("docs/product.md")
    assert is_bundle_md("docs/architecture/billing.md")


def test_root_md_outside_docs_is_not_bundle_by_default():
    # `PLAN.md` at the root doesn't match `docs/**/*.md` and isn't on
    # the agent allowlist — a custom `spec.include` or `spec: true`
    # frontmatter is needed to pull it in. This forces the author to
    # decide, which is the desired friction.
    assert not is_bundle_md("PLAN.md")
    assert not is_bundle_md("notes/scratch.md")


# ---------------------------------------------------------------------------
# Step 4 — agent allowlist
# ---------------------------------------------------------------------------


def test_root_agents_md_is_bundle():
    assert is_bundle_md("AGENTS.md")
    assert is_bundle_md("agents.md")  # case-insensitive
    assert is_bundle_md("Agents.md")


def test_nested_agents_md_is_bundle():
    # Same convention applied to a sub-package.
    assert is_bundle_md("backend/app/AGENTS.md")
    assert is_bundle_md("services/api/CLAUDE.md")


def test_copilot_instructions_is_bundle():
    assert is_bundle_md(".github/copilot-instructions.md")


# ---------------------------------------------------------------------------
# Step 5 — human denylist (after agent allowlist, before default)
# ---------------------------------------------------------------------------


def test_root_readme_is_not_bundle():
    assert not is_bundle_md("README.md")
    assert not is_bundle_md("readme.md")
    assert not is_bundle_md("Readme.md")


def test_changelog_is_not_bundle():
    assert not is_bundle_md("CHANGELOG.md")
    assert not is_bundle_md("docs/CHANGELOG.md")  # at any depth


def test_human_doc_inside_docs_is_excluded():
    # `docs/README.md` matches the default include glob but the
    # filename hits the denylist, which beats the include-by-glob
    # default. The user can pull it back in with `spec.include` or
    # frontmatter.
    assert not is_bundle_md("docs/README.md")
    assert not is_bundle_md("docs/CONTRIBUTING.md")


# ---------------------------------------------------------------------------
# Step 3 — `spec.include` beats the human denylist (PLAN.md §2.1 q1)
# ---------------------------------------------------------------------------


def test_explicit_include_beats_denylist():
    # User says "I want this README to be a spec doc."
    manifest = {"spec": {"include": ["docs/README.md", "docs/**/*.md"]}}
    assert is_bundle_md("docs/README.md", manifest=manifest)


def test_exclude_beats_default_include():
    manifest = {
        "spec": {"include": ["docs/**/*.md"], "exclude": ["docs/internal/**/*.md"]}
    }
    assert is_bundle_md("docs/product.md", manifest=manifest)
    assert not is_bundle_md("docs/internal/scratch.md", manifest=manifest)


def test_custom_include_excludes_default_glob_paths():
    # User narrows to `specs/**/*.md` only — `docs/product.md` is no
    # longer bundle content (the agent allowlist still catches root
    # `AGENTS.md`).
    manifest = {"spec": {"include": ["specs/**/*.md"]}}
    assert is_bundle_md("specs/auth.md", manifest=manifest)
    assert not is_bundle_md("docs/product.md", manifest=manifest)
    assert is_bundle_md("AGENTS.md", manifest=manifest)


# ---------------------------------------------------------------------------
# Step 1 — frontmatter override (highest priority)
# ---------------------------------------------------------------------------


def test_frontmatter_true_pulls_in_an_otherwise_excluded_file():
    # `PLAN.md` at root would normally be auxiliary; frontmatter wins.
    fm = {"spec": True}
    assert is_bundle_md("PLAN.md", frontmatter=fm)


def test_frontmatter_false_excludes_an_otherwise_included_file():
    # Author opts a `docs/*.md` out without changing `spec.include`.
    fm = {"spec": False}
    assert not is_bundle_md("docs/product.md", frontmatter=fm)


def test_frontmatter_with_include_key():
    # `spec.include: true` shorthand inside the frontmatter mapping.
    fm = {"spec": {"include": True}}
    assert is_bundle_md("PLAN.md", frontmatter=fm)
    fm_off = {"spec": {"include": False}}
    assert not is_bundle_md("docs/product.md", frontmatter=fm_off)


def test_frontmatter_unrelated_keys_dont_change_resolution():
    # Frontmatter without `spec` must not flip the answer.
    fm = {"title": "My Spec", "tags": ["billing"]}
    assert is_bundle_md("docs/product.md", frontmatter=fm)
    assert not is_bundle_md("README.md", frontmatter=fm)


# ---------------------------------------------------------------------------
# Non-markdown files
# ---------------------------------------------------------------------------


def test_non_md_returns_false_from_is_bundle_md():
    # The resolver only opines on `.md`. `.prompts` and `spec.yaml`
    # have their own answer (always in) — handled by `is_bundle_path`.
    assert not is_bundle_md("logo.png")
    assert not is_bundle_md("src/app.py")
    assert not is_bundle_md("prompts/foo.prompts")
    assert not is_bundle_md("spec.yaml")


def test_is_bundle_path_handles_non_md_kinds():
    # `is_bundle_path` is the convenience wrapper used by `spec status`
    # and the file tree.
    assert is_bundle_path("spec.yaml")
    assert is_bundle_path("prompts/captured/2026-04-30.prompts")
    assert is_bundle_path("docs/product.md")
    assert is_bundle_path("AGENTS.md")
    assert not is_bundle_path("README.md")
    assert not is_bundle_path("logo.png")


# ---------------------------------------------------------------------------
# Resolver order — explicit case to catch an accidental ladder rewrite
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel,expected,description",
    [
        # Frontmatter beats every later rule
        ("README.md", True, "frontmatter true overrides denylist"),
        # Without frontmatter, denylist excludes
        ("README.md", False, "no frontmatter → denylist wins"),
    ],
)
def test_frontmatter_priority_over_denylist(rel, expected, description):
    fm = {"spec": True} if expected else None
    assert is_bundle_md(rel, frontmatter=fm) == expected, description


def test_include_priority_over_agent_allowlist_is_irrelevant():
    # Agent allowlist and `spec.include` agree (both say "in") in the
    # natural case. The interesting case is exclude-vs-allowlist:
    # explicit `spec.exclude` beats the agent allowlist (the user is
    # opting out a built-in default).
    manifest = {"spec": {"exclude": ["AGENTS.md"]}}
    assert not is_bundle_md("AGENTS.md", manifest=manifest)
