"""
Tiny YAML frontmatter parser, scoped to what the bundle-membership
resolver needs.

The compiler ships a richer parser in ``spec_compiler.frontmatter`` (it
validates the ``spec:`` block, surfaces unknown keys, etc.). The CLI
only needs the *yes / no* answer of "does this file's frontmatter
override its bundle membership?" — so this module is intentionally
forgiving:

  - Files without frontmatter return an empty dict (no override).
  - YAML errors return an empty dict — we never fail a `spec status`
    on a malformed frontmatter block; the compiler will surface the
    error at compile time where it actually matters.
  - We only read the ``spec:`` key; everything else passes through
    untouched (Jekyll / Obsidian / Eleventy are unaffected).
"""

from __future__ import annotations

import re
from typing import Any

import yaml


_FRONTMATTER = re.compile(r"\A---\s*\n(?P<body>.*?)\n---\s*(?:\n|\Z)", re.DOTALL)


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Return the parsed frontmatter dict, or ``{}`` on no frontmatter / error.

    Only the top-level mapping is returned. The resolver only reads
    ``data["spec"]``; other keys are preserved but ignored here.
    """
    match = _FRONTMATTER.match(text)
    if not match:
        return {}
    raw = match.group("body")
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def read_frontmatter(path) -> dict[str, Any]:
    """Convenience: open `path`, parse its frontmatter, return the dict.

    Errors (file missing, decode error) return ``{}`` — the resolver
    falls through to the next step. This is the right fail-quiet
    posture for `spec status`, which has to render *something* even
    when one file in the tree is broken.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    return parse_frontmatter(text)


__all__ = ["parse_frontmatter", "read_frontmatter"]
