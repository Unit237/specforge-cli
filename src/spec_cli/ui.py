"""
Rich-based output helpers.

The CLI stays quiet by default. Color is signal, not decoration:
  - mint  → a deliberate success (matches `site-button-mint` in the web UI)
  - red   → a reject (never a warning, always a hard no)
  - amber → a soft warning (the command still ran; read this)
  - dim   → metadata (paths, hashes, timestamps)
  - cyan  → pointer text (URLs, codes the user has to copy)
"""

from __future__ import annotations

import sys

from rich.console import Console
from rich.theme import Theme

_theme = Theme(
    {
        "sf.mint": "bold #3ddab4",
        "sf.reject": "bold #ff5a6a",
        "sf.warn": "bold #f0b86e",
        "sf.muted": "dim #9aa3b2",
        "sf.point": "bold #7de3ff",
        "sf.label": "bold #c7c9d1",
    }
)

console = Console(theme=_theme, highlight=False, soft_wrap=False)
err_console = Console(theme=_theme, stderr=True, highlight=False, soft_wrap=False)


def ok(msg: str) -> None:
    console.print(f"[sf.mint]✓[/] {msg}")


def info(msg: str) -> None:
    console.print(msg)


def dim(msg: str) -> None:
    console.print(f"[sf.muted]{msg}[/]")


def reject(msg: str) -> None:
    err_console.print(f"[sf.reject]✗[/] {msg}")


def warn(msg: str) -> None:
    """Soft warning — the command did something, the user should still read this."""
    err_console.print(f"[sf.warn]![/] {msg}")


def fatal(msg: str, code: int = 1) -> None:
    reject(msg)
    sys.exit(code)


def pointer(label: str, value: str) -> None:
    console.print(f"[sf.label]{label}[/] [sf.point]{value}[/]")
