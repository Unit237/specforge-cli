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

from .constants import MANIFEST_FILENAME, PROMPTS_DIRNAME, is_spec_file


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
    return idx


def save_index(idx: Index) -> None:
    idx.dir.mkdir(parents=True, exist_ok=True)
    gi = idx.dir / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")
    with idx.path.open("w", encoding="utf-8") as f:
        json.dump({"staged": idx.staged, "pushed": idx.pushed}, f, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Working tree walk
# ---------------------------------------------------------------------------


def walk_spec_files(root: Path) -> Iterable[Path]:
    """
    Yield every file inside `root` that `is_spec_file` would accept, in
    deterministic order. Skips dotfile dirs (`.git`, `.spec`, …).
    """
    root = root.resolve()
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        # Skip anything inside a dot-prefixed directory.
        try:
            parts = p.relative_to(root).parts
        except ValueError:
            continue
        if any(part.startswith(".") for part in parts[:-1]):
            continue
        rel = rel_posix(root, p)
        if is_spec_file(rel):
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


def classify_working_tree(root: Path, idx: Index) -> list[StatusLine]:
    from .constants import classify

    lines: list[StatusLine] = []
    seen: set[str] = set()

    for p in walk_all_files(root):
        rel = rel_posix(root, p)
        seen.add(rel)

        if not is_spec_file(rel):
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
      - exactly one `spec.yaml` at the root
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
