"""Strip terminal noise and disallowed controls from text that becomes TOML.

Claude Code log lines sometimes contain ANSI escape sequences (``\\x1b...``).
Those are fine in a terminal but break Python's ``tomllib`` when they appear
inside TOML **multiline literal** strings (``'''...'''``), which is how we
render long ``text`` fields. We normalize before render and at the capture
adapter so ``spec prompts submit`` can parse the file.
"""

from __future__ import annotations

import re

# CSI and common 2-byte sequences (sgr, cursor, etc.)
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[\][()#%][^\n\x1b]*|[@-Z\\-_])"
)
# OSC can end in BEL or ST
_OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)")


def strip_ansi_escapes(s: str) -> str:
    """Remove ANSI/VT escape sequences; leave newlines and tabs intact."""
    s = _OSC_RE.sub("", s)
    s = _ANSI_ESCAPE_RE.sub("", s)
    return s.replace("\x1b", "")


def sanitize_for_toml_text(s: str) -> str:
    """Make string safe for our TOML emit + ``tomllib`` parse (multiline literals).

    Strips ANSI, then drops other C0 control characters except ``\\n``, ``\\t``,
    and ``\\r`` (so CRLF is preserved).
    """
    s = strip_ansi_escapes(s)
    out: list[str] = []
    for ch in s:
        o = ord(ch)
        if o < 0x20 and ch not in "\n\t\r":
            continue
        if o == 0x7F:  # DEL
            continue
        out.append(ch)
    return "".join(out)
