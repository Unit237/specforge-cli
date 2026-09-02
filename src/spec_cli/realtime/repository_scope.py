"""Canonical repository targeting for machine-wide Spec Live telemetry."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any


def resolve_root(value: Any) -> Path | None:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        return None
    try:
        return Path(value).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def resolve_cwd(value: Any) -> Path | None:
    """Resolve an absolute working directory; relative telemetry is ambiguous."""
    if not isinstance(value, (str, Path)) or not str(value).strip():
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        return None
    return resolve_root(candidate)


def resolve_touched_path(
    value: Any,
    *,
    cwd: Path | None,
    default_root: Path | None = None,
) -> Path | None:
    """Resolve path telemetry without guessing a repository for relative data."""
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return None
    candidate = Path(value.strip()).expanduser()
    if not candidate.is_absolute():
        base = cwd or default_root
        if base is None:
            return None
        candidate = base / candidate
    try:
        return candidate.resolve()
    except (OSError, RuntimeError):
        return None


def path_targets_root(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def roots_touched_by_paths(
    values: Iterable[Any],
    *,
    cwd: Path | None,
    roots: Iterable[Path],
) -> set[Path]:
    candidates = tuple(roots)
    touched: set[Path] = set()
    for value in values:
        path = resolve_touched_path(value, cwd=cwd)
        if path is None:
            continue
        touched.update(root for root in candidates if path_targets_root(path, root))
    return touched


__all__ = [
    "path_targets_root",
    "resolve_cwd",
    "resolve_root",
    "resolve_touched_path",
    "roots_touched_by_paths",
]
