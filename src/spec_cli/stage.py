"""
The CLI's local index — a tiny `.spec/index.json` that records which
paths the user has `spec add`-ed since the last push.

We intentionally do NOT try to be git. We track:

  - staged[path]  = sha256 of the content at stage time
  - pushed[path]  = sha256 of the content last successfully pushed

From these two plus the current working-tree content, `spec status` can
classify every file as staged / stale / unstaged-changes / untracked / clean without any
network.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
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


# Directory names we never recurse into during the working-tree walk,
# even when ``.gitignore`` is silent. The unifying property: dependency
# caches and build outputs that no sane bundle ever wants staged.
# Mirrors the well-known set git itself special-cases under
# ``core.excludesFile`` plus the per-language conventions every modern
# project ships with. Dotfile dirs (``.git``, ``.spec``, …) are skipped
# by a separate rule and don't need to repeat here.
ALWAYS_SKIP_DIRNAMES: frozenset[str] = frozenset(
    {
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        "dist",
        "build",
        "out",
        "target",
        "vendor",
        ".next",
        ".nuxt",
        ".cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "site-packages",
        "coverage",
        ".coverage",
        ".gradle",
        ".idea",
        ".vscode",
    }
)


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


def ensure_root_manifest_staged(idx: Index) -> None:
    """Put ``spec.yaml`` back into *idx.staged* when it exists on disk but was cleared.

    After a successful ``spec push``, uploaded paths move from *staged* to *pushed*.
    Git pre-commit only runs ``spec add`` on paths in the current commit, so an
    unchanged manifest never re-enters *staged* — yet every Cloud snapshot must
    include it. Call this before :func:`assert_push_invariants` whenever there
    is something to push.
    """
    path = idx.root / MANIFEST_FILENAME
    if not path.is_file() or MANIFEST_FILENAME in idx.staged:
        return
    idx.staged[MANIFEST_FILENAME] = sha256(path.read_bytes())
    save_index(idx)


def prune_stale_index_entries(idx: Index, *, manifest: dict | None = None) -> int:
    """Drop ``idx.staged`` / ``idx.pushed`` entries that no longer belong.

    Two failure modes the local index can drift into over time:

      * Files that an older CLI accidentally swept up (e.g. before this
        version started skipping ``node_modules/``) sit in ``pushed``
        forever, polluting ``spec status`` and tempting fresh
        ``spec add .`` invocations to re-stage them.
      * Files that have been deleted from the working tree but never
        explicitly removed from spec — ``pushed`` keeps the old hash
        even though the file is gone.

    For each tracked path we check, in order:

      1. Path skipped by the directory walker (``node_modules`` &
         friends, dotfile dirs)? Drop it.
      2. Wrong extension for a bundle (per ``is_spec_file``)? Drop it.
      3. ``.md`` / ``.markdown`` that fails the bundle resolver under
         the *current* manifest? Drop it.
      4. File no longer present on disk? Drop it from ``pushed`` only —
         a deleted-but-staged path is still meaningful (a subsequent
         push will fail loudly), but ``pushed`` should reflect what's
         live.

    The server keeps whatever was uploaded; this is a *local* tidy-up.
    Returns the number of entries removed across both maps so callers
    can decide whether to log anything.
    """
    removed = 0
    root = idx.root.resolve()

    def _drop(rel: str) -> bool:
        parts = tuple(PurePosixPath(rel).parts)
        if _path_is_skipped(parts):
            return True
        if not is_spec_file(rel):
            return True
        suffix = PurePosixPath(rel).suffix.lower()
        if suffix in (".md", ".markdown"):
            abs_path = root / rel
            fm = read_frontmatter(abs_path) if abs_path.is_file() else None
            if not is_bundle_path(rel, manifest=manifest, frontmatter=fm):
                return True
        return False

    for rel in list(idx.staged):
        if _drop(rel):
            idx.staged.pop(rel, None)
            removed += 1

    for rel in list(idx.pushed):
        if _drop(rel):
            idx.pushed.pop(rel, None)
            removed += 1
            continue
        # Drop pushed entries whose file is gone — keeps the index in
        # sync with the working tree without touching the server copy.
        # Staged entries point at content the user explicitly chose to
        # ship, so a missing-file there stays put and surfaces at push
        # time instead.
        abs_path = root / rel
        if not abs_path.is_file():
            idx.pushed.pop(rel, None)
            removed += 1

    if removed:
        save_index(idx)
    return removed


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


def _path_is_skipped(parts: tuple[str, ...]) -> bool:
    """True if any directory segment in ``parts[:-1]`` is dotfile-prefixed
    or matches the always-skip list (``node_modules``, ``dist``, …).

    The trailing element (``parts[-1]``) is the file's own name, so a
    file *named* ``node_modules`` (rare but legal) would still be
    considered. We only filter on directory ancestry.
    """
    for part in parts[:-1]:
        if not part:
            continue
        if part.startswith("."):
            return True
        if part in ALWAYS_SKIP_DIRNAMES:
            return True
    return False


def _git_ls_tracked_and_untracked(root: Path) -> list[Path] | None:
    """Use ``git ls-files`` to enumerate the working tree honoring
    ``.gitignore``.

    Returns the list of every file git considers tracked or untracked-
    but-not-ignored, anchored at ``root``. Returns ``None`` when ``root``
    is not inside a git worktree, when ``git`` is missing, or when the
    subprocess fails — the caller falls back to filesystem walking.

    We pass ``--cached --others --exclude-standard`` to mirror exactly
    what ``git status`` would surface: tracked files (so ``spec add .``
    re-stages real edits) + untracked-not-ignored files (so freshly
    written ``docs/foo.md`` is picked up immediately) − ignored files
    (so ``node_modules/`` and ``dist/`` stay out). ``-z`` keeps NUL
    separators so paths with newlines never get mis-split.
    """
    if shutil.which("git") is None:
        return None
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout or b""
    if not raw:
        return []
    out: list[Path] = []
    seen: set[str] = set()
    for chunk in raw.split(b"\0"):
        if not chunk:
            continue
        try:
            rel = chunk.decode("utf-8")
        except UnicodeDecodeError:
            continue
        rel = rel.replace("\\", "/").strip()
        if not rel or rel in seen:
            continue
        seen.add(rel)
        p = (root / rel).resolve()
        if not p.is_file():
            continue
        out.append(p)
    out.sort(key=lambda p: str(p))
    return out


def _filesystem_walk(root: Path) -> Iterable[Path]:
    """Recursive working-tree walk that prunes ``ALWAYS_SKIP_DIRNAMES``
    and dotfile directories before descending.

    Used as the fallback when git isn't available. Pruning at the
    directory level (not per file) avoids the multi-second hit from
    ``rglob('*')`` over a populated ``node_modules``.
    """
    if not root.is_dir():
        return
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if entry.is_dir():
                if name.startswith(".") and entry != root:
                    continue
                if name in ALWAYS_SKIP_DIRNAMES:
                    continue
                stack.append(entry)
            elif entry.is_file():
                yield entry


def walk_spec_files(
    root: Path, *, manifest: dict | None = None
) -> Iterable[Path]:
    """
    Yield every file inside `root` that belongs in the bundle, in
    deterministic order. Skips dotfile dirs (`.git`, `.spec`, …),
    well-known dependency / build trees (``node_modules``, ``dist``,
    ``__pycache__``, …), and — when ``root`` is inside a git worktree
    and the ``git`` binary is available — anything ``.gitignore``
    excludes.

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
    abs_root = root.resolve()
    for p in _iter_walk_candidates(abs_root):
        try:
            parts = p.relative_to(abs_root).parts
        except ValueError:
            continue
        if _path_is_skipped(parts):
            continue
        rel = rel_posix(abs_root, p)
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
    """Yield every file under `root` (for `status` to report non-spec).

    Same pruning rules as :func:`walk_spec_files` — dotfile dirs,
    well-known dependency trees, and anything ``.gitignore`` excludes
    when running inside a git worktree. ``spec status`` is meant to
    surface what would land in a push, so showing a thousand
    ``node_modules`` rows is just noise the user has to scroll past.
    """
    abs_root = root.resolve()
    for p in _iter_walk_candidates(abs_root):
        try:
            parts = p.relative_to(abs_root).parts
        except ValueError:
            continue
        if _path_is_skipped(parts):
            continue
        yield p


def _iter_walk_candidates(root: Path) -> Iterable[Path]:
    """Backbone of both walkers — defer to ``git ls-files`` when in a
    repo, otherwise filesystem-walk with the same pruning rules.

    Caller is responsible for resolving ``root`` first so the absolute
    paths returned by ``git ls-files`` line up with the ``relative_to``
    arithmetic the walkers do downstream.

    The git-aware path is what makes ``spec add .`` honor whatever the
    user already typed into ``.gitignore`` (build outputs, vendored
    deps, secrets, …) without us having to re-implement gitignore
    parsing. The filesystem fallback covers fresh checkouts and
    non-git workflows; both produce paths sorted by string for stable
    output.
    """
    via_git = _git_ls_tracked_and_untracked(root)
    if via_git is not None:
        yield from via_git
        return
    yield from _filesystem_walk(root)


# ---------------------------------------------------------------------------
# Status classification
# ---------------------------------------------------------------------------


@dataclass
class StatusLine:
    rel: str
    kind: str              # "md" | "prompts" | "settings" | "other"
    state: str             # "staged" | "staged_stale" | "unstaged_modified" | "untracked" | "clean" | "ignored" | "deleted"
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
                # Like git: staged snapshot exists but the working tree moved on.
                lines.append(StatusLine(rel=rel, kind=kind, state="staged_stale", hash=h))
        elif rel in idx.pushed:
            if idx.pushed[rel] == h:
                lines.append(StatusLine(rel=rel, kind=kind, state="clean", hash=h))
            else:
                # Tracked from a prior push but not in the current staged set (or
                # staged entry was cleared); disk differs from last push — run spec add.
                lines.append(StatusLine(rel=rel, kind=kind, state="unstaged_modified", hash=h))
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
