"""Machine-wide workday switch for Spec Live.

``spec live`` keeps the precise per-bundle controls. This module owns the
human-scale control engineers use every day:

* ``spec on`` enables sharing + autostart and starts every known local bundle.
* ``spec off`` mutes sharing, disables autostart, and gracefully stops them.
* ``spec status`` calls :func:`print_workday_status` before its bundle status.

Known bundles live in ``~/.spec/preferences.json`` and are learned by normal
``spec init`` / ``spec watch`` / ``spec live start|ensure`` use. The registry
contains paths only; no repository content leaves the bundle through it.
"""
from __future__ import annotations

import os
from pathlib import Path

import click

from ..config import (
    BundleNotFoundError,
    discover_bundle_roots_under_cwd,
    find_bundle_root,
    load_credentials,
)
from ..preferences import Preferences, load_preferences
from ..realtime import WatcherStartError, is_running, start_in_background, stop_daemon
from ..realtime.active_edits import ActiveEditsStore
from ..ui import console, dim, ok, warn


def _current_bundle_roots() -> list[Path]:
    try:
        return [find_bundle_root().resolve()]
    except BundleNotFoundError as exc:
        # ``find_bundle_root`` intentionally refuses to choose between peer
        # bundles. The machine-wide switch has no such ambiguity: all peers
        # below a workspace folder are exactly what it should register.
        if "Multiple Spec bundles" in str(exc):
            return discover_bundle_roots_under_cwd(Path.cwd())
        return []


def _known_bundle_roots(
    prefs: Preferences,
    *,
    include_current: bool,
    prune: bool,
) -> tuple[list[Path], int]:
    """Return valid, unique registered roots plus a stale-entry count."""
    values = list(prefs.bundles)
    current_roots = _current_bundle_roots() if include_current else []
    for current in current_roots:
        if str(current) not in values:
            values.append(str(current))

    roots: list[Path] = []
    stale = 0
    for value in values:
        root = Path(value).expanduser().resolve()
        if not (root / "spec.yaml").is_file():
            stale += 1
            continue
        if root not in roots:
            roots.append(root)

    roots.sort(key=lambda path: str(path).lower())
    normalized = [str(root) for root in roots]
    if prune and normalized != prefs.bundles:
        prefs.bundles = normalized
        prefs.save()
    return roots, stale


def _missing_agent_rules(root: Path) -> bool:
    """Whether a bundle still needs the generated coordination rules."""
    agents = root / "AGENTS.md"
    cursor = root / ".cursor" / "rules" / "spec-team-presence.mdc"
    claude = root / ".claude" / "settings.json"
    try:
        agents_ready = agents.is_file() and "spec live coordination" in agents.read_text(
            encoding="utf-8"
        ).lower()
    except OSError:
        agents_ready = False
    return not (agents_ready and cursor.is_file() and claude.is_file())


def _release_local_locks(root: Path) -> int:
    store = ActiveEditsStore(root)
    removed = 0
    for lock in store.list():
        if store.release(lock.id):
            removed += 1
    return removed


def print_workday_status(*, include_bundles: bool = True) -> None:
    """Render machine policy and every known local watcher."""
    prefs = load_preferences()
    roots, stale = _known_bundle_roots(
        prefs,
        include_current=True,
        prune=False,
    )
    running = [(root, is_running(root)) for root in roots]
    running_count = sum(record is not None for _, record in running)
    is_on = not prefs.prompt_stream_muted and not prefs.autostart_disabled

    state = "ON" if is_on else "OFF"
    console.print(f"[sf.label]Spec workday[/] [bold]{state}[/]")
    dim(
        f"  sharing: {'enabled' if not prefs.prompt_stream_muted else 'muted'}"
        f" · autostart: {'enabled' if not prefs.autostart_disabled else 'disabled'}"
        f" · watchers: {running_count}/{len(roots)} running"
    )
    if os.environ.get("SPEC_NO_AUTOSTART", "").strip() == "1":
        warn("SPEC_NO_AUTOSTART=1 is set in this shell; automatic starts are suppressed.")
    if stale:
        dim(f"  {stale} stale bundle registration(s) will be pruned by `spec on` or `spec off`.")

    if not include_bundles:
        return
    if not roots:
        dim("  no bundles registered yet — run `spec on` from a Spec bundle once.")
        return
    for root, record in running:
        if record is None:
            dim(f"  ○ {root} · stopped")
        else:
            dim(f"  ● {root} · pid {record.pid}")


@click.command("on")
def workday_on_cmd() -> None:
    """Turn Spec on for this machine and start known bundle watchers.

    This is the start-of-workday command. It removes the machine mute,
    enables shell autostart, remembers the current bundle (when applicable),
    prunes missing bundle paths, and idempotently starts each known watcher.
    Per-bundle ``cloud.prompt_stream`` policy remains authoritative.
    """
    prefs = load_preferences()
    prefs.prompt_stream = "default"
    prefs.autostart = "default"
    roots, stale = _known_bundle_roots(prefs, include_current=True, prune=True)
    prefs.save()

    creds = load_credentials()
    if not creds or not creds.access_token:
        ok("Spec is ON for this machine.")
        warn("No Spec Cloud login found; watchers will start after `spec login`.")
        print_workday_status()
        return

    started = 0
    already_running = 0
    failures: list[tuple[Path, str]] = []
    missing_rules: list[Path] = []
    for root in roots:
        if _missing_agent_rules(root):
            missing_rules.append(root)
        try:
            outcome = start_in_background(root)
        except WatcherStartError as exc:
            failures.append((root, str(exc)))
            continue
        if outcome.already_running:
            already_running += 1
        else:
            started += 1

    ok(
        "Spec is ON for this machine "
        f"({started} started, {already_running} already running)."
    )
    if stale:
        dim(f"  pruned {stale} missing bundle registration(s).")
    for root, message in failures:
        warn(f"Could not start {root}: {message}")
    if missing_rules:
        warn(
            f"{len(missing_rules)} bundle(s) need current agent rules; run "
            "`spec init --upgrade-rules` in each listed bundle."
        )
        for root in missing_rules:
            dim(f"  {root}")
    print_workday_status()


@click.command("off")
def workday_off_cmd() -> None:
    """Turn Spec off for this machine and stop every known watcher.

    This is the end-of-workday command. It first disables new automatic
    starts, then gracefully stops local daemons so each can publish its final
    clean presence state, and finally releases leftover advisory edit locks.
    Cloud PR automation is independent and remains available while laptops
    are offline.
    """
    prefs = load_preferences()
    roots, stale = _known_bundle_roots(prefs, include_current=True, prune=False)
    prefs.prompt_stream = "muted"
    prefs.autostart = "off"
    prefs.bundles = [str(root) for root in roots]
    prefs.save()

    stopped = 0
    idle = 0
    forced = 0
    locks_released = 0
    for root in roots:
        outcome = stop_daemon(root)
        if outcome.was_running:
            stopped += 1
            if outcome.killed:
                forced += 1
        else:
            idle += 1
        locks_released += _release_local_locks(root)

    ok(
        "Spec is OFF for this machine "
        f"({stopped} stopped, {idle} already stopped, {locks_released} locks released)."
    )
    if forced:
        warn(f"{forced} watcher(s) required a forced stop after the grace period.")
    if stale:
        dim(f"  pruned {stale} missing bundle registration(s).")
    dim("  This controls local watchers only; cloud-side jobs are configured separately.")
    print_workday_status()


__all__ = ["print_workday_status", "workday_off_cmd", "workday_on_cmd"]
