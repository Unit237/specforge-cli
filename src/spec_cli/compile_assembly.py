"""
Assembly of a single self-contained "compile prompt" — the blob handed to
Claude Code (or to `spec-compile` via API mode).

This module is intentionally dependency-light and does not import the
compiler package. `spec compile` must work on a machine that has only
the CLI installed — the whole point of the Claude-Code-first paradigm is
that the user's existing agent provides the inference.

The assembly is deterministic: same bundle → same bytes. This matters so a
compile prompt can be hashed, reviewed, and reproduced.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .config import Manifest
from .constants import PROMPTS_DIRNAME
from .prompts.tiers import Tier, iter_compilable
from .stage import rel_posix


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass
class AssembledBundle:
    """Everything the compiler needs to know about the bundle, in memory.

    ``prompts_files`` is kept as (rel, content) for backwards compatibility
    with any callers that don't care about tiers, but the renderer uses
    ``curated_prompts`` + ``captured_prompts`` so the compile prompt can
    separate authoritative intent from advisory scrollback. Files under
    ``prompts/curated/_pending/`` are *excluded* — a prompt awaiting review
    has no business changing what the compiler produces.
    """

    name: str
    description: str
    output_target: str
    spec_files: list[tuple[str, str]] = field(default_factory=list)     # (rel, content)
    prompts_files: list[tuple[str, str]] = field(default_factory=list)  # (rel, raw TOML) — deprecated mirror of curated + captured
    curated_prompts: list[tuple[str, str]] = field(default_factory=list)   # (rel, content) — authoritative
    captured_prompts: list[tuple[str, str]] = field(default_factory=list)  # (rel, content) — advisory


# ---------------------------------------------------------------------------
# Spec walking — mirrors the compiler's logic so the CLI's pre-assembly
# matches what the user would get from `spec-compile`.
# ---------------------------------------------------------------------------


def _is_md(rel: str) -> bool:
    low = rel.lower()
    return low.endswith(".md") or low.endswith(".markdown")


def _match_any(rel: str, patterns: list[str]) -> bool:
    p = PurePosixPath(rel)
    for pat in patterns:
        if fnmatch.fnmatchcase(rel, pat):
            return True
        # `docs/**/*.md` should also match `docs/product.md`. fnmatch on
        # its own doesn't implement `**`, so we emulate by also comparing
        # against each parent-collapsed pattern.
        if "**" in pat:
            # Convert `a/**/b` into a regex-ish pattern by treating `**`
            # as "any path". Simple approach: split and check.
            if _double_star_match(rel, pat):
                return True
        _ = p  # keep PurePosixPath import used if we extend later
    return False


def _double_star_match(rel: str, pattern: str) -> bool:
    """Very small subset of glob `**`: treats `**` as "zero or more path
    components". Sufficient for the patterns `spec.yaml` users write
    (`docs/**/*.md`, `prompts/**/*.md`)."""
    rel_parts = rel.split("/")
    pat_parts = pattern.split("/")

    def match(ri: int, pi: int) -> bool:
        if pi == len(pat_parts):
            return ri == len(rel_parts)
        if pat_parts[pi] == "**":
            # Match zero or more parts.
            for k in range(ri, len(rel_parts) + 1):
                if match(k, pi + 1):
                    return True
            return False
        if ri == len(rel_parts):
            return False
        if fnmatch.fnmatchcase(rel_parts[ri], pat_parts[pi]):
            return match(ri + 1, pi + 1)
        return False

    return match(0, 0)


def _collect_spec_files(root: Path, manifest: Manifest) -> list[str]:
    """Return bundle-relative paths of every `.md` to compile, in order.

    Order is:
      1. `spec.entry` first (if it exists).
      2. Then everything matching `spec.include`, in filesystem order.
      3. Anything matching `spec.exclude` is removed.
      4. Duplicates removed; first occurrence wins.

    Mirrors `spec_compiler.bundle.load_bundle` semantics for v0.1.
    """
    spec = (manifest.data.get("spec") or {}) if manifest.data else {}
    entry = spec.get("entry") or "docs/product.md"
    include = spec.get("include") or ["docs/**/*.md"]
    exclude = spec.get("exclude") or []

    ordered: list[str] = []
    seen: set[str] = set()

    if (root / entry).is_file():
        ordered.append(entry)
        seen.add(entry)

    for path in sorted((root).rglob("*")):
        if not path.is_file():
            continue
        rel = rel_posix(root, path)
        # Skip dotfile dirs and the sessions subdir — sessions get their
        # own assembly path, they're not spec docs.
        parts = PurePosixPath(rel).parts
        if any(p.startswith(".") for p in parts[:-1]):
            continue
        if not _is_md(rel):
            continue
        if not _match_any(rel, include):
            continue
        if _match_any(rel, exclude):
            continue
        # Don't double-count the entry.
        if rel in seen:
            continue
        # Anything under `prompts/` is not a spec doc — prompts live in
        # `.prompts` files, not `.md`, and markdown under prompts/ is
        # rejected elsewhere in the stack anyway.
        if rel.startswith(PROMPTS_DIRNAME + "/") or rel == PROMPTS_DIRNAME:
            continue
        ordered.append(rel)
        seen.add(rel)

    return ordered


def _collect_prompts_by_tier(root: Path) -> tuple[list[str], list[str]]:
    """Return (curated_rel_paths, captured_rel_paths).

    - Curated = reviewer-approved (`prompts/curated/*.prompts`) plus legacy
      files at the prompts root (grandfathered).
    - Captured = auto-captured scrollback (`prompts/captured/*.prompts`).
    - Pending (`prompts/curated/_pending/*.prompts`) is silently excluded;
      those files exist to be reviewed, not compiled.

    Each list is already in deterministic filename order, and the two lists
    are disjoint. Callers that want the old flat view can concatenate them.
    """
    curated: list[str] = []
    captured: list[str] = []
    for tp in iter_compilable(root):
        if tp.tier in (Tier.CURATED, Tier.LEGACY):
            curated.append(tp.rel)
        elif tp.tier == Tier.CAPTURED:
            captured.append(tp.rel)
    return curated, captured


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def assemble_bundle(root: Path, manifest: Manifest) -> AssembledBundle:
    """Walk the bundle and read every file the compile prompt needs."""
    output = (manifest.data.get("output") or {}) if manifest.data else {}
    bundle = AssembledBundle(
        name=manifest.name or root.name,
        description=(manifest.data.get("description") or "").strip(),
        output_target=output.get("target") or "./out",
    )
    for rel in _collect_spec_files(root, manifest):
        bundle.spec_files.append((rel, (root / rel).read_text(encoding="utf-8")))

    curated, captured = _collect_prompts_by_tier(root)
    for rel in curated:
        content = (root / rel).read_text(encoding="utf-8")
        bundle.curated_prompts.append((rel, content))
        bundle.prompts_files.append((rel, content))
    for rel in captured:
        content = (root / rel).read_text(encoding="utf-8")
        bundle.captured_prompts.append((rel, content))
        bundle.prompts_files.append((rel, content))
    return bundle


# ---------------------------------------------------------------------------
# Rendering the compile prompt
# ---------------------------------------------------------------------------


_SYSTEM_INTRO = """\
You are compiling a Spec bundle.

You will be given:
  1. Zero or more **curated `.prompts` files** — conversational history a
     human reviewer has approved as meaningful context. Treat these as
     first-class intent alongside the specs.
  2. Zero or more **captured `.prompts` files** — raw scrollback auto-written
     by the capture tool. Treat these as low-signal background context:
     useful for disambiguating intent, but never authoritative. Prefer the
     specs and curated prompts when they disagree.
  3. One or more **spec documents** — plain-English descriptions of what
     to build. Read them in order.

Produce the code that implements the specs. Write each generated file to
the bundle's output directory (see OUTPUT below).

OUTPUT — non-negotiable:
  - Write generated files under `{output_target}` (paths relative to the
    bundle root).
  - Preserve the directory structure the specs imply.
  - If you use tool calls (e.g. Write), write directly. If you are
    responding in a non-tool context, emit each file as a
    `<file path="relative/path">...</file>` block; any prose outside
    those blocks is ignored as commentary.
  - Never write outside `{output_target}` unless a spec explicitly
    requests a different location (e.g. `.github/workflows/*.yml`).
"""


def _render_prompt_section(
    parts: list[str],
    heading: str,
    intro: str,
    files: list[tuple[str, str]],
) -> None:
    if not files:
        return
    parts.append("")
    parts.append(heading)
    parts.append("")
    parts.append(intro)
    for rel, content in files:
        parts.append("")
        parts.append(f"### `{rel}`")
        parts.append("")
        parts.append("```toml")
        parts.append(content.rstrip())
        parts.append("```")


def render_compile_prompt(bundle: AssembledBundle) -> str:
    """Render the deterministic compile-prompt blob."""
    parts: list[str] = []
    parts.append(f"# Spec compile — {bundle.name}")
    if bundle.description:
        parts.append("")
        parts.append(bundle.description.strip())
    parts.append("")
    parts.append("## Instructions")
    parts.append("")
    parts.append(_SYSTEM_INTRO.format(output_target=bundle.output_target).rstrip())

    _render_prompt_section(
        parts,
        "## Curated prompt history",
        (
            "Each block below is a `.prompts` file that a human reviewer approved "
            "via `spec prompts review`. User turns convey intent; "
            "`[commit]` metadata ties the session to a real commit. Treat these "
            "as source, not telemetry."
        ),
        bundle.curated_prompts,
    )

    _render_prompt_section(
        parts,
        "## Captured prompt history (advisory)",
        (
            "Auto-captured scrollback from agent sessions — no human review. Use "
            "sparingly: they help disambiguate intent when the specs are terse, "
            "but a spec or curated prompt always wins."
        ),
        bundle.captured_prompts,
    )

    parts.append("")
    parts.append("## Specs")
    for rel, content in bundle.spec_files:
        parts.append("")
        parts.append(f"### `{rel}`")
        parts.append("")
        parts.append(content.rstrip())

    return "\n".join(parts).rstrip() + "\n"
