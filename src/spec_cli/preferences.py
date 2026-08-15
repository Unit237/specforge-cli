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
      "bundles":       ["/absolute/path/to/a/spec/bundle"],
      "discovery_roots": ["/absolute/path/to/a/workspace"]
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

* ``discovery_roots`` — workspace roots scanned by a rootless
  ``spec discover`` and by ``spec on``. This is what makes discovery a
  machine-wide inventory instead of a command whose meaning changes with the
  current directory.

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
_TRANSIENT_BUNDLE_DIRS = frozenset({".codex-worktrees"})


def is_transient_bundle_root(value: str | Path) -> bool:
    """Return whether VALUE belongs to an agent's disposable worktree area."""
    parts = {part.casefold() for part in Path(value).expanduser().parts}
    return bool(parts & _TRANSIENT_BUNDLE_DIRS)


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
    discovery_roots: list[str] = None  # type: ignore[assignment]
    raw: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.bundles is None:
            self.bundles = []
        if self.discovery_roots is None:
            self.discovery_roots = []
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
                if (
                    isinstance(value, str)
                    and value
                    and not is_transient_bundle_root(value)
                    and value not in bundles
                ):
                    bundles.append(value)
        discovery_roots_raw = data.get("discovery_roots")
        discovery_roots: list[str] = []
        if isinstance(discovery_roots_raw, list):
            for value in discovery_roots_raw:
                if isinstance(value, str) and value and value not in discovery_roots:
                    discovery_roots.append(value)
        return cls(
            prompt_stream=ps,
            autostart=au,
            bundles=bundles,
            discovery_roots=discovery_roots,
            raw=data,
        )

    # ── writes ────────────────────────────────────────────────

    def save(self) -> Path:
        path = _prefs_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.info("spec-prefs: could not create %s: %s", path.parent, e)
            return path
        try:
            os.chmod(path.parent, stat.S_IRWXU)  # 0700
        except OSError:
            pass

        merged = dict(self.raw or {})
        merged["schema"] = PREFERENCES_SCHEMA_VERSION
        merged["prompt_stream"] = self.prompt_stream
        merged["autostart"] = self.autostart
        merged["bundles"] = list(dict.fromkeys(self.bundles))
        merged["discovery_roots"] = list(dict.fromkeys(self.discovery_roots))

        try:
            tmp_fd, tmp_name = tempfile.mkstemp(
                prefix=f"{PREFERENCES_FILENAME}.",
                suffix=".tmp",
                dir=str(path.parent),
            )
        except OSError as e:
            log.info("spec-prefs: could not create temp file in %s: %s", path.parent, e)
            return path
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


def _machine_broadcast_roots(prefs: Preferences) -> list[Path]:
    roots: list[Path] = []
    for raw in prefs.bundles:
        if is_transient_bundle_root(raw):
            continue
        try:
            root = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if (root / "spec.yaml").is_file() and root not in roots:
            roots.append(root)
    return sorted(roots, key=lambda root: str(root).casefold())


def machine_broadcast_owner(prefs: Preferences | None = None) -> Path | None:
    """Return the one registered watcher that owns machine-wide prompts.

    ``spec on`` starts one watcher per registered bundle, but local agent
    stores are machine-wide. Electing one stable owner lets that watcher scan
    every conversation exactly once while the remaining watchers keep their
    repository presence duties. The machine mute/autostart pair is the
    authoritative workday switch; outside that state there is no owner and a
    directly-invoked watcher keeps its legacy per-project behavior.
    """
    current = prefs or load_preferences()
    if current.prompt_stream_muted or current.autostart_disabled:
        return None
    roots = _machine_broadcast_roots(current)
    return roots[0] if roots else None


def machine_broadcast_role(
    bundle_root: Path, prefs: Preferences | None = None
) -> str | None:
    """Return ``owner`` / ``member`` for an active ``spec on`` registry."""
    current = prefs or load_preferences()
    if current.prompt_stream_muted or current.autostart_disabled:
        return None
    roots = _machine_broadcast_roots(current)
    root = bundle_root.expanduser().resolve()
    if not roots or root not in roots:
        return None
    return "owner" if root == roots[0] else "member"


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
    if is_transient_bundle_root(root):
        return prefs
    if root not in prefs.bundles:
        prefs.bundles.append(root)
        prefs.save()
    return prefs


def remember_discovery_root(search_root: Path) -> Preferences:
    """Remember one workspace as part of the machine-wide discovery scope."""
    root = str(search_root.expanduser().resolve())
    prefs = load_preferences()
    if root not in prefs.discovery_roots:
        prefs.discovery_roots.append(root)
        prefs.save()
    return prefs


__all__ = [
    "PREFERENCES_FILENAME",
    "PREFERENCES_SCHEMA_VERSION",
    "Preferences",
    "is_transient_bundle_root",
    "load_preferences",
    "machine_broadcast_owner",
    "machine_broadcast_role",
    "remember_bundle",
    "remember_discovery_root",
]
