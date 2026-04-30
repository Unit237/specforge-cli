"""
The CLI's local index — a tiny `.spec/index.json` that records which
paths the user has `spec add`-ed since the last push.

We intentionally do NOT try to be git. We track:

  - staged[path]  = sha256 of the content at stage time
  - pushed[path]  = sha256 of the content last successfully pushed

From these two plus the current working-tree content, `spec status` can
classify every file as staged / modified / untracked / clean without any
network.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable

from .constants import (
    MANIFEST_FILENAME,
    PROMPTS_DIRNAME,
    is_bundle_path,
    is_spec_file,
)
from .frontmatter import read_frontmatter


INDEX_DIRNAME = ".spec"
INDEX_FILENAME = "index.json"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rel_posix(root: Path, path: Path) -> str:
    """Relative posix path from bundle root, for storage + server keys."""
    return PurePosixPath(path.resolve().relative_to(root.resolve())).as_posix()


@dataclass
class Index:
    root: Path
    staged: dict[str, str] = field(default_factory=dict)
    pushed: dict[str, str] = field(default_factory=dict)
    # Every absolute filesystem path this bundle has lived at, oldest
    # first. Append-only and travels with the folder (it's stored
    # inside the bundle's own ``.spec/index.json``). The Claude Code
    # and Cursor adapters consult this list so a folder rename doesn't
    # orphan a session — see ``record_bundle_path``.
    bundle_paths: list[str] = field(default_factory=list)

    @property
    def dir(self) -> Path:
        return self.root / INDEX_DIRNAME

    @property
    def path(self) -> Path:
        return self.dir / INDEX_FILENAME


def load_index(root: Path) -> Index:
    idx = Index(root=root)
    if idx.path.is_file():
        with idx.path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        idx.staged = dict(raw.get("staged") or {})
        idx.pushed = dict(raw.get("pushed") or {})
        raw_paths = raw.get("bundle_paths") or []
        if isinstance(raw_paths, list):
            idx.bundle_paths = [p for p in raw_paths if isinstance(p, str) and p]
    return idx


def save_index(idx: Index) -> None:
    idx.dir.mkdir(parents=True, exist_ok=True)
    gi = idx.dir / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")
    with idx.path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "staged": idx.staged,
                "pushed": idx.pushed,
                "bundle_paths": idx.bundle_paths,
            },
            f,
            indent=2,
            sort_keys=True,
        )


def record_bundle_path(root: Path) -> Index:
    """Remember the absolute path the bundle currently lives at.

    The first time this is called for a bundle (typically by
    ``spec init`` and again by every ``spec prompts capture`` run), the
    resolved path is appended to ``index.bundle_paths``. Subsequent
    calls are no-ops if the path is already recorded.

    On a rename / move the file moves with the folder, so the next
    ``capture`` from the new location appends the new path next to the
    old one. Both Claude Code and Cursor session lookups iterate the
    full list, which is what makes prompt history survive a folder
    move (Fix #2 of the git-parity work).

    Returns the updated ``Index`` so callers that already needed it for
    other reasons don't pay for two reads.
    """
    idx = load_index(root)
    resolved = str(root.resolve())
    if resolved not in idx.bundle_paths:
        idx.bundle_paths.append(resolved)
        save_index(idx)
    return idx


def historical_bundle_paths(root: Path) -> list[Path]:
    """Every filesystem path this bundle has lived at, current first.

    Order: the resolved current root, then any earlier paths recorded
    in ``index.bundle_paths`` that are *not* the current path. We
    deliberately keep paths in the list even if they no longer exist
    on disk — the bundle has moved, but Claude Code's session store
    very likely still has data under the old encoded name and we need
    to look there.
    """
    current = root.resolve()
    out: list[Path] = [current]
    seen: set[Path] = {current}
    try:
        idx = load_index(root)
    except (OSError, ValueError, json.JSONDecodeError):
        return out
    for raw in idx.bundle_paths:
        candidate = Path(raw)
        if not candidate.is_absolute():
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
    return out


# ---------------------------------------------------------------------------
# Working tree walk
# ---------------------------------------------------------------------------


def walk_spec_files(
    root: Path, *, manifest: dict | None = None
) -> Iterable[Path]:
    """
    Yield every file inside `root` that belongs in the bundle, in
    deterministic order. Skips dotfile dirs (`.git`, `.spec`, …).

    Bundle-membership is resolved through `is_bundle_path`:
      - `spec.yaml` — always in.
      - `.prompts`  — always in.
      - `.md` / `.markdown` — in iff the resolver says so (frontmatter
        override → spec.yaml include/exclude → agent allowlist → human
        denylist). See `constants.is_bundle_md` for the full ladder.

    Caller can pass a parsed manifest dict to honour the bundle's
    `spec.include` / `spec.exclude` configuration. With `manifest=None`
    the resolver falls back to the default include glob, which matches
    most bundles, so the explicit `spec add path/to/file.md` flow still
    works without loading the manifest twice.

    Note: ``.cursor/rules/**/*.mdc`` is intentionally **not** yielded by
    this walk even though the resolver knows about it — the dotfile-dir
    skip is the right default for ``spec add .`` (it would otherwise
    sweep up ``.git/``, ``.venv/``, ``.spec/``). Users who want to
    include cursor rules can do so explicitly via
    ``spec add .cursor/rules/foo.mdc`` or by listing the path in
    ``spec.include``.
    """
    root = root.resolve()
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        try:
            parts = p.relative_to(root).parts
        except ValueError:
            continue
        if any(part.startswith(".") for part in parts[:-1]):
            continue
        rel = rel_posix(root, p)
        if not is_spec_file(rel):
            continue
        # `.prompts` and `spec.yaml` are always in; only `.md`-class
        # files need the full resolver (which can read frontmatter).
        suffix = PurePosixPath(rel).suffix.lower()
        if suffix in (".md", ".markdown"):
            fm = read_frontmatter(p)
            if not is_bundle_path(rel, manifest=manifest, frontmatter=fm):
                continue
        yield p


def walk_all_files(root: Path) -> Iterable[Path]:
    """Yield every file under `root` (for `status` to report non-spec)."""
    root = root.resolve()
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        parts = p.relative_to(root).parts
        if any(part.startswith(".") for part in parts[:-1]):
            continue
        yield p


# ---------------------------------------------------------------------------
# Status classification
# ---------------------------------------------------------------------------


@dataclass
class StatusLine:
    rel: str
    kind: str              # "md" | "prompts" | "settings" | "other"
    state: str             # "staged" | "modified" | "untracked" | "clean" | "ignored" | "deleted"
    hash: str | None = None


def classify_working_tree(
    root: Path, idx: Index, *, manifest: dict | None = None
) -> list[StatusLine]:
    from .constants import classify

    lines: list[StatusLine] = []
    seen: set[str] = set()

    for p in walk_all_files(root):
        rel = rel_posix(root, p)
        seen.add(rel)

        if not is_spec_file(rel):
            lines.append(StatusLine(rel=rel, kind="other", state="ignored"))
            continue

        # An `.md` that fails the bundle-membership resolver is treated
        # the same as any other non-spec file: visible in the tree,
        # rendered as `ignored`, never swept up by `spec add .`. This
        # matches the design call in PLAN.md §2.1 — non-bundle `.md`
        # files share a row state with `.png` rather than getting their
        # own "auxiliary" surface.
        suffix = PurePosixPath(rel).suffix.lower()
        if suffix in (".md", ".markdown"):
            fm = read_frontmatter(p)
            if not is_bundle_path(rel, manifest=manifest, frontmatter=fm):
                lines.append(StatusLine(rel=rel, kind="other", state="ignored"))
                continue

        content = p.read_bytes()
        h = sha256(content)
        kind = classify(rel)

        if rel in idx.staged:
            if idx.staged[rel] == h:
                lines.append(StatusLine(rel=rel, kind=kind, state="staged", hash=h))
            else:
                lines.append(StatusLine(rel=rel, kind=kind, state="modified", hash=h))
        elif rel in idx.pushed:
            if idx.pushed[rel] == h:
                lines.append(StatusLine(rel=rel, kind=kind, state="clean", hash=h))
            else:
                lines.append(StatusLine(rel=rel, kind=kind, state="modified", hash=h))
        else:
            lines.append(StatusLine(rel=rel, kind=kind, state="untracked", hash=h))

    # Files we've tracked before that no longer exist on disk.
    tracked = set(idx.staged) | set(idx.pushed)
    for rel in sorted(tracked - seen):
        lines.append(StatusLine(rel=rel, kind=classify(rel), state="deleted"))

    return lines


# ---------------------------------------------------------------------------
# Manifest invariants (run at push time)
# ---------------------------------------------------------------------------


class InvalidBundleError(ValueError):
    pass


def assert_push_invariants(root: Path, staged: dict[str, str]) -> None:
    """
    At push time the snapshot we're about to send must contain:
      - exactly one `spec.yaml` at the root (no nested ones — the
        manifest is bundle-scoped, so a `backend/app/spec.yaml` is
        application config that happens to share a filename, not a
        sub-manifest)
      - at least one `.md` / `.markdown` file
      - no `.md` files inside `prompts/` — that directory is reserved
        for `.prompts` (plural) files and mixing types there is almost
        always a misplaced spec doc or a copy-paste error

    Intermediate saves can be broken (§6 of PLAN), but push is where we
    fail loud so the server never has to.
    """
    if MANIFEST_FILENAME not in staged:
        raise InvalidBundleError(
            f"The bundle has no {MANIFEST_FILENAME} staged. "
            "Run `spec add spec.yaml`."
        )

    # Defense in depth: with current `is_spec_file` semantics a nested
    # `spec.yaml` is filtered out at `spec add`-time and never reaches
    # the index. But indexes outlive code (someone upgrades the CLI
    # mid-flight, hand-edits `.spec/index.json`, or carries forward a
    # stale entry from before this rule was tightened), so we re-check
    # here rather than letting the server return its less-actionable
    # "Only .md / .markdown / .prompts files (and spec.yaml) are
    # allowed in a bundle" rejection.
    nested_manifests = [
        rel
        for rel in staged
        if rel != MANIFEST_FILENAME
        and PurePosixPath(rel).name == MANIFEST_FILENAME
    ]
    if nested_manifests:
        path = nested_manifests[0]
        raise InvalidBundleError(
            f"{path} — `{MANIFEST_FILENAME}` is only allowed at the bundle "
            "root. Each bundle has exactly one manifest, so a nested "
            f"`{MANIFEST_FILENAME}` is application config that happens "
            "to share a filename, not a sub-manifest.\n\n"
            f"Run `spec unstage {path}` to drop it (and consider renaming "
            "the file, e.g. to `config.yaml`, so it doesn't get re-added "
            "by a future `spec add .`)."
        )

    md_prefix = f"{PROMPTS_DIRNAME}/"
    for rel in staged:
        lower = rel.lower()
        if rel.startswith(md_prefix) and (
            lower.endswith(".md") or lower.endswith(".markdown")
        ):
            raise InvalidBundleError(
                f"{rel} — `.md` files are not allowed inside `{PROMPTS_DIRNAME}/`. "
                "Prompts live in `.prompts` files (plural). Move the file, "
                "or run `spec prompts capture` to write a fresh one.\n\n"
                "That path is still in your staged set from an earlier `spec add`, "
                "so the next `spec push` includes it even if you only just staged "
                "a different file. Run `spec status` to list staged paths, then "
                f"`spec unstage {rel}` to drop it (or unstage a whole directory)."
            )

    md_count = sum(
        1
        for rel in staged
        if rel != MANIFEST_FILENAME and is_spec_file(rel)
    )
    if md_count == 0:
        raise InvalidBundleError(
            "The bundle has no .md files staged. Add at least one spec doc."
        )
