"""
Shared extension allow-list.

This file is intentionally small and dependency-free so the Spec Cloud
API, the CLI, and the compiler can all pin to the same values. If you fork,
keep these three repos in agreement — the server is still the source of truth,
but fast-failing on the client saves a round-trip.
"""

from __future__ import annotations

from pathlib import PurePosixPath

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


def is_spec_file(path: str | PurePosixPath) -> bool:
    """
    True if `path` is something Spec will accept into a bundle.

    Accepts either a forward-slash path or a PurePosixPath. Case-insensitive
    on the extension, case-sensitive on the manifest filename (YAML convention).
    """
    p = PurePosixPath(str(path))
    if p.name == MANIFEST_FILENAME:
        return True
    return p.suffix.lower() in SPEC_EXTENSIONS


def classify(path: str | PurePosixPath) -> str:
    """
    Return one of: "settings" | "prompts" | "md".

    Mirrors the `kind` enum on the server-side `bundle_files` table so the
    CLI's idea of a file's kind matches Cloud's without a network call.

    Classification rules, in order:
      1. `spec.yaml` at any depth is `settings`.
      2. Any `.prompts` file is `prompts`, regardless of location.
      3. Everything else spec-eligible is `md`.

    Note: there is no "prompts live under prompts/" rule. Prompts are
    identified strictly by their extension. `.md` files under `prompts/`
    are rejected elsewhere in the stack.
    """
    p = PurePosixPath(str(path))
    if p.name == MANIFEST_FILENAME:
        return "settings"
    if p.suffix.lower() == ".prompts":
        return "prompts"
    return "md"
