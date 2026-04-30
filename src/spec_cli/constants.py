"""
Shared extension allow-list and bundle-membership resolver.

This file is intentionally small and dependency-free so the Spec Cloud
API, the CLI, and the compiler can all pin to the same values. If you fork,
keep these three repos in agreement — the server is still the source of truth,
but fast-failing on the client saves a round-trip.

Two related but distinct questions live here:

  1. ``is_spec_file(path)`` — "does this *kind* of file ever belong in a
     bundle?" — pure-extension gate used at ``spec add``-time and on the
     server's accept/reject path.
  2. ``is_bundle_md(rel, *, manifest, frontmatter)`` — "should this
     specific ``.md`` count as bundle content (vs. an auxiliary doc like
     a ``README``)?" — the resolver used by ``spec status``, the
     compiler, and the Cloud file tree to surface only the docs that
     actually shape the build.

The resolver is a 5-step, first-match-wins ladder. See ``is_bundle_md``
for the canonical implementation; the table at the top of that function
is the source of truth for what's surfaced where.
"""

from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath
from typing import Any, Mapping

# The only file extensions a Spec bundle will ever accept.
#
# Three classes, no overlap (see docs/prompt-format.md):
#
#   - `.md` / `.markdown` — specs (intent prose). Never prompts; prompts
#                           have their own extension.
#   - `.prompts`          — a commit's conversational sessions, bundled into
#                           one TOML file. Schema: spec.prompts/v0.1.
#   - `spec.yaml`    — the one-per-bundle compiler manifest.
SPEC_EXTENSIONS: frozenset[str] = frozenset({".md", ".markdown", ".prompts"})

# The bundle manifest. Exactly one of these, at the bundle root.
MANIFEST_FILENAME: str = "spec.yaml"

# The conventional directory for `.prompts` files. Not enforced — a `.prompts`
# file anywhere in the bundle is valid — but this is where `spec prompts
# capture` writes.
PROMPTS_DIRNAME: str = "prompts"

# Two-tier layout under `prompts/`. Prompt files live in one of these places:
#
#   prompts/captured/<name>.prompts   Auto-written by `spec prompts capture`.
#                                     Low-signal advisory context for the compiler.
#                                     Do not hand-edit expecting review; this is
#                                     scrollback, not source.
#
#   prompts/curated/<name>.prompts    Reviewer-approved, authoritative context.
#                                     The compiler treats these as first-class
#                                     intent alongside the spec docs.
#
#   prompts/curated/_pending/<name>.prompts
#                                     Submitted for review, not yet accepted.
#                                     Excluded from compile. Presence here is what
#                                     `spec prompts check --ci` gates on.
#
#   prompts/<name>.prompts            Legacy — grandfathered as curated.
#                                     Still read by the compiler; `capture` no
#                                     longer writes here.
PROMPTS_CAPTURED_DIRNAME: str = "captured"
PROMPTS_CURATED_DIRNAME: str = "curated"
PROMPTS_PENDING_DIRNAME: str = "_pending"

# Largest drag-drop / batch upload size. Keep in sync with
# POST /api/projects/{id}/files/batch on the server.
MAX_BATCH_SIZE: int = 10


# ---------------------------------------------------------------------------
# Bundle membership: agent allowlist + human denylist
# ---------------------------------------------------------------------------
#
# The two lists below answer the question "is this `.md` part of the
# bundle, or just a sibling doc that happens to live in the worktree?"
# Names are matched case-insensitively (PLAN.md §2.1) — the conventions
# in the wild are uppercase, but `Agents.md` and `agents.md` are real
# variants we don't want to bikeshed about.
#
# When a new convention emerges, add it here in *all three* repos
# (spec-cli, spec-compiler, spec backend + frontend). There's no shared
# package gating drift — the lists are short enough to mirror by hand
# (PLAN.md §2.1 open question 2).

# Files we treat as bundle content regardless of location. The unifying
# property: their explicit purpose is to instruct an AI agent or human
# implementer about the codebase. Compare with HUMAN_DOC_FILENAMES.
AGENT_INSTRUCTION_FILENAMES: frozenset[str] = frozenset(
    {
        "agents.md",       # openagents.dev convention (Cursor, Codex, …)
        "claude.md",       # Anthropic Claude Code
        "gemini.md",       # Google Gemini Code Assist
        "llms.txt",        # llmstxt.org convention
        "llms-full.txt",   # llmstxt.org full variant
    }
)

# Patterns matched against the full bundle-relative path (POSIX,
# lowercase). Used for nested conventions where filename alone isn't
# enough.
#
# ``.cursor/rules/**/*.mdc`` is intentionally NOT on this list yet:
# ``.mdc`` isn't on ``SPEC_EXTENSIONS`` either, so the file would be
# rejected at the extension gate before the resolver ever ran. Adding
# it requires bumping the extension allow-list in all three repos
# (spec-cli, spec-compiler, spec backend) and is tracked as a v0.3
# concern. Until then, Cursor users put their rules in ``AGENTS.md``
# at the bundle root — Cursor reads both files at session start.
AGENT_INSTRUCTION_PATTERNS: tuple[str, ...] = (
    ".github/copilot-instructions.md",
)

# Files we exclude from the bundle by default — they're for humans, not
# the build. A user can still pull a specific one back in by listing it
# explicitly in `spec.include` (which beats this list per PLAN.md §2.1
# open question 1) or by adding `spec: true` frontmatter.
HUMAN_DOC_FILENAMES: frozenset[str] = frozenset(
    {
        "readme.md",
        "readme.markdown",
        "changelog.md",
        "contributing.md",
        "code_of_conduct.md",
        "security.md",
        "license",
        "license.md",
        "license.txt",
        "notice",
        "notice.md",
        "history.md",
        "roadmap.md",
    }
)

# Default include glob when `spec.include` is unset / empty in the
# manifest. Mirrors `spec init`'s scaffolded value.
DEFAULT_SPEC_INCLUDE: tuple[str, ...] = ("docs/**/*.md",)


def is_spec_file(path: str | PurePosixPath) -> bool:
    """
    True if `path` is something Spec will accept into a bundle.

    Accepts either a forward-slash path or a PurePosixPath. Case-insensitive
    on the extension, case-sensitive on the manifest filename (YAML convention).

    `spec.yaml` is recognised **only at the bundle root** — there is exactly
    one manifest per bundle. A nested `backend/app/spec.yaml` is treated as
    a regular `.yaml` file (i.e. *not* a spec file) so the staging walker
    silently skips it instead of staging something the server will later
    reject. Catching it here keeps the misleading "wrong extension" error
    off the push path.
    """
    p = PurePosixPath(str(path))
    if str(p) == MANIFEST_FILENAME:
        return True
    return p.suffix.lower() in SPEC_EXTENSIONS


def classify(path: str | PurePosixPath) -> str:
    """
    Return one of: "settings" | "prompts" | "md".

    Mirrors the `kind` enum on the server-side `bundle_files` table so the
    CLI's idea of a file's kind matches Cloud's without a network call.

    Classification rules, in order:
      1. `spec.yaml` **at the bundle root** is `settings`. Nested
         `spec.yaml` files are not bundle content (they're application
         config that happens to share a filename) and are filtered out
         upstream by `is_spec_file`; if one slips through this far it
         falls into the `md`/`prompts`/other branches based on its
         extension and is ultimately rejected by the server.
      2. Any `.prompts` file is `prompts`, regardless of location.
      3. Everything else spec-eligible is `md`.

    Note: there is no "prompts live under prompts/" rule. Prompts are
    identified strictly by their extension. `.md` files under `prompts/`
    are rejected elsewhere in the stack.
    """
    p = PurePosixPath(str(path))
    if str(p) == MANIFEST_FILENAME:
        return "settings"
    if p.suffix.lower() == ".prompts":
        return "prompts"
    return "md"


# ---------------------------------------------------------------------------
# Glob / pattern helpers
# ---------------------------------------------------------------------------
#
# We hand-roll a `**`-aware matcher rather than pull `pathspec` because
# the rules are tiny (.gitignore-flavoured) and we already pin to the
# stdlib for the rest of constants.py. Same semantics as
# `spec_compiler.bundle_glob` so the two repos can mirror tests.


def _glob_match(rel: str, pattern: str) -> bool:
    """`**`-aware glob match for forward-slash paths.

    `*` matches inside one path segment; `**` matches zero or more full
    segments; `?` matches one character. Case-sensitive — callers
    lowercase before calling when they want case-insensitive matching.
    """
    rel_parts = rel.split("/")
    pat_parts = pattern.split("/")

    def go(ri: int, pi: int) -> bool:
        if pi == len(pat_parts):
            return ri == len(rel_parts)
        head = pat_parts[pi]
        if head == "**":
            if pi + 1 == len(pat_parts):
                return True
            for k in range(ri, len(rel_parts) + 1):
                if go(k, pi + 1):
                    return True
            return False
        if ri == len(rel_parts):
            return False
        if not fnmatch.fnmatchcase(rel_parts[ri], head):
            return False
        return go(ri + 1, pi + 1)

    return go(0, 0)


def _match_any(rel: str, patterns) -> bool:
    return any(_glob_match(rel, p) for p in patterns)


# ---------------------------------------------------------------------------
# Bundle membership resolver
# ---------------------------------------------------------------------------


def _is_agent_instruction(rel_lower: str) -> bool:
    """True if the lowercased path matches the agent-instruction
    allowlist. Filename-only conventions match at any depth; pattern
    conventions match against the full path."""
    name = PurePosixPath(rel_lower).name
    if name in AGENT_INSTRUCTION_FILENAMES:
        return True
    return _match_any(rel_lower, AGENT_INSTRUCTION_PATTERNS)


def _is_human_doc(rel_lower: str) -> bool:
    """True if the lowercased filename matches the built-in human-doc
    denylist. Matches at any depth — `docs/README.md` is excluded just
    like a root `README.md`."""
    name = PurePosixPath(rel_lower).name
    return name in HUMAN_DOC_FILENAMES


def is_bundle_md(
    rel: str | PurePosixPath,
    *,
    manifest: Mapping[str, Any] | None = None,
    frontmatter: Mapping[str, Any] | None = None,
) -> bool:
    """Resolve whether `rel` (a `.md` / `.markdown` file) is bundle content.

    The 6-step ladder, first match wins (PLAN.md §2.1):

      1. **Frontmatter override.** ``spec: true`` / ``spec: false``
         under the existing ``spec:`` frontmatter key (or ``include``
         shorthand) wins over everything else.
      2. **`spec.exclude` match.** The user typed it; respect it. Out.
      3. **Explicit `spec.include` match.** User set ``spec.include``
         in the manifest and the path matches → in. Beats the
         human-doc denylist (PLAN.md §2.1 q1: explicit beats default).
      4. **Agent allowlist.** ``AGENTS.md`` / ``CLAUDE.md`` /
         ``llms.txt`` / ``.github/copilot-instructions.md`` → in.
         Files whose explicit purpose is to instruct the build,
         regardless of location.
      5. **Human denylist.** ``README.md`` / ``CHANGELOG.md`` /
         ``CONTRIBUTING.md`` / ``LICENSE`` / etc. → out. Beats the
         *default* ``docs/**/*.md`` glob, but loses to an *explicit*
         include or to frontmatter.
      6. **Default include glob.** ``docs/**/*.md`` → in. The natural
         home for spec docs in a fresh bundle.

    Anything that falls through (e.g. a custom ``spec.include`` that
    didn't match) is auxiliary (out). The CLI surfaces it as
    ``ignored`` in ``spec status`` — same row as a ``.png``.

    `manifest` is the parsed ``spec.yaml`` mapping (or ``None``). Only
    the ``spec.include`` and ``spec.exclude`` lists are read; everything
    else is ignored. ``frontmatter`` is the YAML frontmatter parsed
    out of the file (the ``spec:`` key, optionally) — pass ``None`` if
    the caller hasn't parsed it. Frontmatter parsing is the caller's
    responsibility because it requires reading the file.
    """
    rel_str = str(rel)
    rel_lower = rel_str.lower()
    if not (rel_lower.endswith(".md") or rel_lower.endswith(".markdown")):
        # Not a markdown file at all — the resolver only opines on `.md`.
        # The caller decides what to do with non-markdown.
        return False

    # Step 1: frontmatter override.
    if frontmatter is not None and isinstance(frontmatter, Mapping):
        spec_fm = frontmatter.get("spec")
        if isinstance(spec_fm, bool):
            return spec_fm
        if isinstance(spec_fm, Mapping):
            inc = spec_fm.get("include")
            if isinstance(inc, bool):
                return inc

    spec_section: Mapping[str, Any] = {}
    if manifest is not None:
        raw_spec = manifest.get("spec")
        if isinstance(raw_spec, Mapping):
            spec_section = raw_spec

    # Distinguish "user did not set include" (use default at step 6) from
    # "user explicitly set include" (use at step 3, beats denylist). The
    # difference matters: a `docs/CHANGELOG.md` should not be bundle
    # content under the default glob, but should be if the user typed
    # `docs/CHANGELOG.md` into spec.include.
    raw_include = spec_section.get("include")
    has_explicit_include = isinstance(raw_include, list) and bool(raw_include)
    explicit_include_globs: tuple[str, ...] = (
        tuple(p for p in raw_include if isinstance(p, str))
        if has_explicit_include
        else ()
    )

    raw_exclude = spec_section.get("exclude")
    exclude_globs: tuple[str, ...] = (
        tuple(p for p in raw_exclude if isinstance(p, str))
        if isinstance(raw_exclude, list)
        else ()
    )

    # Step 2: spec.yaml exclude.
    if exclude_globs and _match_any(rel_str, exclude_globs):
        return False

    # Step 3: explicit spec.yaml include.
    if explicit_include_globs and _match_any(rel_str, explicit_include_globs):
        return True

    # Step 4: agent-instruction allowlist (case-insensitive).
    if _is_agent_instruction(rel_lower):
        return True

    # Step 5: human-doc denylist (case-insensitive). Beats the *default*
    # include glob below, loses to an explicit include above.
    if _is_human_doc(rel_lower):
        return False

    # Step 6: default include glob — only when the user did NOT set
    # `spec.include`. If they did, we already evaluated it at step 3.
    if not has_explicit_include and _match_any(rel_str, DEFAULT_SPEC_INCLUDE):
        return True

    return False


def is_bundle_path(
    rel: str | PurePosixPath,
    *,
    manifest: Mapping[str, Any] | None = None,
    frontmatter: Mapping[str, Any] | None = None,
) -> bool:
    """Convenience: is this *any* path part of the bundle?

    Wraps `is_bundle_md` with the trivial cases for `spec.yaml` and
    `.prompts` files (always in if they pass the extension gate). Used
    by `spec status` and the Cloud file tree to render the file's row
    state in one call.

    `spec.yaml` is bundle content **only at the root** — see
    `is_spec_file` for why nested manifests are intentionally excluded.
    """
    p = PurePosixPath(str(rel))
    if str(p) == MANIFEST_FILENAME:
        return True
    suffix = p.suffix.lower()
    if suffix == ".prompts":
        return True
    if suffix in (".md", ".markdown"):
        return is_bundle_md(str(rel), manifest=manifest, frontmatter=frontmatter)
    return False
