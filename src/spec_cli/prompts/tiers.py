"""
Two-tier layout helpers for `.prompts` files.

A bundle's `prompts/` directory is split into three well-known buckets:

  prompts/captured/          Auto-captured from agent scrollback. Low signal.
                             Included by the compiler as advisory context.
  prompts/curated/           Reviewer-approved. Authoritative context.
  prompts/curated/_pending/  Submitted, not yet reviewed. Invisible to the
                             compiler, and the presence of any file here is
                             what the CI gate keys on.

Files that predate the split (`prompts/<name>.prompts`, no subdirectory) are
grandfathered as `curated` so existing bundles keep working.

Every helper in this module returns bundle-relative POSIX paths or absolute
`Path`s consistently — the CLI mostly wants relative strings for logging,
the compile assembler mostly wants absolute paths for reading bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Iterator

from ..constants import (
    PROMPTS_CAPTURED_DIRNAME,
    PROMPTS_CURATED_DIRNAME,
    PROMPTS_DIRNAME,
    PROMPTS_PENDING_DIRNAME,
)
from ..stage import rel_posix


class Tier(str, Enum):
    """Which review bucket a `.prompts` file is in."""

    CAPTURED = "captured"
    CURATED = "curated"
    PENDING = "pending"
    LEGACY = "legacy"  # `prompts/<name>.prompts`, grandfathered as curated


@dataclass(frozen=True)
class TieredPrompt:
    tier: Tier
    abs_path: Path
    rel: str  # bundle-relative POSIX


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------


def prompts_root(bundle_root: Path) -> Path:
    return bundle_root / PROMPTS_DIRNAME


def captured_dir(bundle_root: Path) -> Path:
    return prompts_root(bundle_root) / PROMPTS_CAPTURED_DIRNAME


def curated_dir(bundle_root: Path) -> Path:
    return prompts_root(bundle_root) / PROMPTS_CURATED_DIRNAME


def pending_dir(bundle_root: Path) -> Path:
    return curated_dir(bundle_root) / PROMPTS_PENDING_DIRNAME


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_tier(rel: str) -> Tier | None:
    """Classify a bundle-relative POSIX path into its tier.

    Returns `None` if the path isn't under `prompts/` or doesn't look like a
    `.prompts` file. Callers that want "any prompt, anywhere" should use
    `iter_all_prompts` instead.

    The check for `_pending` has to happen before `curated` because
    `_pending/` is a subdirectory of `curated/`.
    """
    p = PurePosixPath(rel)
    if p.suffix.lower() != ".prompts":
        return None
    parts = p.parts
    if not parts or parts[0] != PROMPTS_DIRNAME:
        return None
    rest = parts[1:]
    if not rest:
        return None
    if (
        len(rest) >= 3
        and rest[0] == PROMPTS_CURATED_DIRNAME
        and rest[1] == PROMPTS_PENDING_DIRNAME
    ):
        return Tier.PENDING
    if rest[0] == PROMPTS_CAPTURED_DIRNAME and len(rest) >= 2:
        return Tier.CAPTURED
    if rest[0] == PROMPTS_CURATED_DIRNAME and len(rest) >= 2:
        return Tier.CURATED
    if len(rest) == 1:
        return Tier.LEGACY
    return None


# ---------------------------------------------------------------------------
# Iteration
# ---------------------------------------------------------------------------


def _iter_dir(directory: Path) -> Iterator[Path]:
    """Yield every `*.prompts` file directly inside `directory`, sorted.

    Not recursive — each tier lives in a flat directory by design. If a user
    mkdirs a subdirectory under `curated/` (other than `_pending/`) we ignore
    it rather than try to be clever; the format is flat until we have a
    reason to change that.
    """
    if not directory.is_dir():
        return
    for p in sorted(directory.glob("*.prompts")):
        if p.is_file():
            yield p


def iter_captured(bundle_root: Path) -> Iterator[TieredPrompt]:
    for p in _iter_dir(captured_dir(bundle_root)):
        yield TieredPrompt(Tier.CAPTURED, p, rel_posix(bundle_root, p))


def iter_curated(bundle_root: Path) -> Iterator[TieredPrompt]:
    for p in _iter_dir(curated_dir(bundle_root)):
        yield TieredPrompt(Tier.CURATED, p, rel_posix(bundle_root, p))


def iter_pending(bundle_root: Path) -> Iterator[TieredPrompt]:
    for p in _iter_dir(pending_dir(bundle_root)):
        yield TieredPrompt(Tier.PENDING, p, rel_posix(bundle_root, p))


def iter_legacy(bundle_root: Path) -> Iterator[TieredPrompt]:
    root = prompts_root(bundle_root)
    if not root.is_dir():
        return
    for p in sorted(root.glob("*.prompts")):
        if p.is_file():
            yield TieredPrompt(Tier.LEGACY, p, rel_posix(bundle_root, p))


def iter_compilable(bundle_root: Path) -> Iterator[TieredPrompt]:
    """Every prompt the compiler should read — curated + legacy + captured.

    Deliberately excludes `pending/`. Order matters for determinism:
    curated first (authoritative), then legacy (grandfathered as curated),
    then captured (advisory). Each bucket is already sorted by filename.
    """
    yield from iter_curated(bundle_root)
    yield from iter_legacy(bundle_root)
    yield from iter_captured(bundle_root)


def iter_all_prompts(bundle_root: Path) -> Iterator[TieredPrompt]:
    """Every `.prompts` file in the bundle, including pending ones."""
    yield from iter_curated(bundle_root)
    yield from iter_legacy(bundle_root)
    yield from iter_captured(bundle_root)
    yield from iter_pending(bundle_root)


# ---------------------------------------------------------------------------
# Counts (cheap summary for `status` / `check`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TierCounts:
    captured: int
    curated: int
    pending: int
    legacy: int

    @property
    def total(self) -> int:
        return self.captured + self.curated + self.pending + self.legacy


def count_tiers(bundle_root: Path) -> TierCounts:
    return TierCounts(
        captured=sum(1 for _ in iter_captured(bundle_root)),
        curated=sum(1 for _ in iter_curated(bundle_root)),
        pending=sum(1 for _ in iter_pending(bundle_root)),
        legacy=sum(1 for _ in iter_legacy(bundle_root)),
    )


__all__ = [
    "Tier",
    "TieredPrompt",
    "TierCounts",
    "captured_dir",
    "classify_tier",
    "count_tiers",
    "curated_dir",
    "iter_all_prompts",
    "iter_captured",
    "iter_compilable",
    "iter_curated",
    "iter_legacy",
    "iter_pending",
    "pending_dir",
    "prompts_root",
]
