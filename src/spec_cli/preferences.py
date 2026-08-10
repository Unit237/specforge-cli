"""
Per-user, machine-local preferences for the Spec CLI.

Lives at ``~/.spec/preferences.json`` (or ``$SPEC_HOME/preferences.json``).
Distinct from credentials on purpose — credentials are *who you are*;
preferences are *how you want the CLI to behave on this machine*.

Shape (small, forward-compat — unknown keys are preserved verbatim):

    {
      "schema": 1,
      "prompt_stream": "default" | "muted",
      "autostart":     "default" | "off",
      "bundles":       ["/absolute/path/to/a/spec/bundle"]
    }

Why JSON, not YAML: the credentials file already uses JSON, so users
who poke at ``~/.spec/`` aren't suddenly switching format. Why a
separate file from credentials: signing in and out should not nuke
behavioural preferences (and credentials get rotated; preferences
don't).

The current keys:

* ``prompt_stream`` — ``"muted"`` silences Spec Live broadcasting on
  this machine even when the bundle's manifest opts in. ``"default"``
  defers to the manifest. The CLI never broadcasts when this is
  ``"muted"`` regardless of any ``spec.yaml`` setting; this is the
  individual-engineer kill-switch.

* ``autostart`` — ``"off"`` disables the shell-hook autostart for
  ``spec watch`` on this machine. Set by ``spec live autostart off``.
  Default is ``"default"`` (autostart on whenever the user enters a
  ``spec init``'d bundle in an interactive shell).

* ``bundles`` — absolute roots of Spec bundles seen on this machine. This is
  the small local registry used by the machine-wide ``spec on`` / ``spec off``
  workday switch. Missing paths are ignored and pruned on the next switch.

Atomic writes (write-temp + rename) and tolerant reads (missing or
malformed file = defaults). Same hygiene as ``LiveCursor`` so a kill
in flight can't corrupt the file.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PREFERENCES_FILENAME = "preferences.json"
PREFERENCES_SCHEMA_VERSION = 1


def _prefs_dir() -> Path:
    override = os.environ.get("SPEC_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".spec"


def _prefs_path() -> Path:
    return _prefs_dir() / PREFERENCES_FILENAME


@dataclass
class Preferences:
    """User-controlled CLI behaviour for this machine.

    Use :meth:`load` to read; mutate the dataclass; persist with
    :meth:`save`. ``raw`` carries any unknown keys forward so an older
    CLI doesn't silently drop a newer CLI's settings on round-trip.
    """

    prompt_stream: str = "default"  # "default" | "muted"
    autostart: str = "default"  # "default" | "off"
    bundles: list[str] = None  # type: ignore[assignment]
    raw: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.bundles is None:
            self.bundles = []
        if self.raw is None:
            self.raw = {}

    # ── reads ────────────────────────────────────────────────

    @property
    def prompt_stream_muted(self) -> bool:
        """Should Spec Live *broadcasting* be silenced on this machine?

        ``True`` overrides any per-bundle ``cloud.prompt_stream:
        enabled``. Receiving (incoming peer events) is unaffected —
        muting is the broadcasting kill-switch only.
        """
        return self.prompt_stream == "muted"

    @property
    def autostart_disabled(self) -> bool:
        """Should the shell-hook autostart for ``spec watch`` be
        skipped on this machine?

        ``True`` means the autostart hook should fall through silently
        regardless of bundle settings. The user can still run
        ``spec watch`` / ``spec live start`` by hand — this only
        suppresses the implicit fire-on-cd behaviour.
        """
        return self.autostart == "off"

    # ── factories ────────────────────────────────────────────

    @classmethod
    def load(cls) -> "Preferences":
        path = _prefs_path()
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.info("spec-prefs: ignoring malformed prefs at %s: %s", path, e)
            return cls()
        if not isinstance(data, dict):
            return cls()
        prompt_stream_raw = data.get("prompt_stream")
        if prompt_stream_raw in ("muted", "default"):
            ps = prompt_stream_raw
        else:
            ps = "default"
        autostart_raw = data.get("autostart")
        if autostart_raw in ("off", "default"):
            au = autostart_raw
        else:
            au = "default"
        bundles_raw = data.get("bundles")
        bundles: list[str] = []
        if isinstance(bundles_raw, list):
            for value in bundles_raw:
                if isinstance(value, str) and value and value not in bundles:
                    bundles.append(value)
        return cls(prompt_stream=ps, autostart=au, bundles=bundles, raw=data)

    # ── writes ────────────────────────────────────────────────

    def save(self) -> Path:
        path = _prefs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, stat.S_IRWXU)  # 0700
        except OSError:
            pass

        merged = dict(self.raw or {})
        merged["schema"] = PREFERENCES_SCHEMA_VERSION
        merged["prompt_stream"] = self.prompt_stream
        merged["autostart"] = self.autostart
        merged["bundles"] = list(dict.fromkeys(self.bundles))

        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=f"{PREFERENCES_FILENAME}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp_name, path)
        except OSError as e:
            log.info("spec-prefs: save failed: %s", e)
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            return path
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass
        return path


def load_preferences() -> Preferences:
    """Module-level convenience — used by ``spec watch`` and the
    ``spec live`` command group so a single import covers the whole
    surface."""
    return Preferences.load()


def remember_bundle(bundle_root: Path) -> Preferences:
    """Add a bundle root to this machine's workday registry.

    Idempotent and intentionally tiny: commands call this only after normal
    bundle discovery has already proved the path is a Spec bundle.
    """
    root = str(bundle_root.expanduser().resolve())
    prefs = load_preferences()
    if root not in prefs.bundles:
        prefs.bundles.append(root)
        prefs.save()
    return prefs


__all__ = [
    "PREFERENCES_FILENAME",
    "PREFERENCES_SCHEMA_VERSION",
    "Preferences",
    "load_preferences",
    "remember_bundle",
]
