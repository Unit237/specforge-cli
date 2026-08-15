"""Stable machine id for Spec Live broadcast attribution.

One ``spec on`` owner now broadcasts conversations for every repository on a
machine. Every foreground ``spec watch`` on that machine must therefore use the
same id; a per-bundle id mislabels the owner's events as ``other machine``.

The id lives outside any repository at ``~/.spec/broadcast-client-id``. It
survives process restarts and differs across physical machines because each
machine has its own home directory. Legacy per-bundle files remain harmless and
are intentionally not read.
"""
from __future__ import annotations

import uuid
from pathlib import Path


def _machine_client_id_path() -> Path:
    return Path.home() / ".spec" / "broadcast-client-id"


def load_or_create_broadcast_client_id(_bundle_root: Path) -> str:
    """Return the stable UUID string for this physical machine.

    ``_bundle_root`` remains in the signature for caller compatibility; machine
    identity deliberately does not depend on it.
    """
    path = _machine_client_id_path()
    try:
        if path.is_file():
            raw = path.read_text(encoding="utf-8").strip()
            if len(raw) >= 8:
                return raw[:128]
    except OSError:
        pass
    token = str(uuid.uuid4())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token + "\n", encoding="utf-8")
    except OSError:
        # No home dir or read-only — still return a process token so POSTs
        # work; echo filtering may not match across restarts.
        return token
    return token


__all__ = ["load_or_create_broadcast_client_id"]
