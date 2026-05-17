"""
Filesystem scope rules: when does a working directory belong to a Spec bundle?

Used by prompt-source adapters (Claude Code, Codex) and kept aligned with
Cursor's workspace intersection rules in ``sources/cursor.py``.
"""

from __future__ import annotations

from pathlib import Path

# Max directory levels between a session ``cwd`` and the bundle root when the
# cwd is an *ancestor* of the bundle (parent monorepo / wrapper folder).
_DEFAULT_MAX_ANCESTOR_DEPTH = 16


def path_intersects_bundle(
    path: Path,
    bundle_root: Path,
    *,
    max_ancestor_depth: int = _DEFAULT_MAX_ANCESTOR_DEPTH,
) -> bool:
    """True when ``path`` is the bundle root, inside it, or a bounded ancestor.

    * **Exact / inside** — ``path`` is the bundle root or a subdirectory
      (developer ran the agent from ``<bundle>/backend``).
    * **Ancestor** — ``path`` is a parent folder that contains the bundle
      (developer ran the agent from a wrapper or monorepo root while editing
      bundle files). Depth is capped so unrelated ``/Users/me`` cwds do not
      match every bundle on disk.
    """
    try:
        resolved = path.expanduser().resolve()
        root = bundle_root.resolve()
    except OSError:
        return False
    if resolved == root:
        return True
    if root in resolved.parents:
        return True
    if resolved in root.parents:
        try:
            return len(root.relative_to(resolved).parts) <= max_ancestor_depth
        except ValueError:
            return False
    return False
