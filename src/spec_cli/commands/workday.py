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

from ..api import ApiError, CloudClient
from ..cloud_binding import CloudBindingError, ensure_cloud_binding
from ..config import (
    BundleNotFoundError,
    discover_bundle_roots_under_cwd,
    find_bundle_root,
    load_credentials,
    load_manifest,
)
from ..preferences import Preferences, is_transient_bundle_root, load_preferences
from ..realtime import WatcherStartError, is_running, start_in_background, stop_daemon
from ..realtime.active_edits import ActiveEditsStore
from ..ui import console, dim, ok, warn
from .init import refresh_agent_rules


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
    for raw in prefs.discovery_roots:
        workspace = Path(raw).expanduser().resolve()
        if workspace.is_dir():
            current_roots.extend(discover_bundle_roots_under_cwd(workspace))
    for current in current_roots:
        if is_transient_bundle_root(current):
            continue
        if str(current) not in values:
            values.append(str(current))

    roots: list[Path] = []
    stale = 0
    for value in values:
        if is_transient_bundle_root(value):
            continue
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


def _bundle_is_cloud_bound(root: Path) -> bool:
    """Whether a watcher has an immutable Cloud project to connect to."""
    try:
        manifest = load_manifest(root)
    except Exception:  # noqa: BLE001 — status must remain fail-open
        return False
    return bool(manifest.cloud_project and manifest.cloud_bundle_id)


def _release_local_locks(root: Path) -> int:
    store = ActiveEditsStore(root)
    removed = 0
    for lock in store.list():
        if store.release(lock.id):
            removed += 1
    return removed


def _cloud_login_error(creds) -> str | None:
    """Preflight the one credential shared by every watcher we are about to start."""
    if not creds or not creds.access_token:
        return "No Spec Cloud login found"
    try:
        CloudClient(creds)._request("GET", "/api/auth/me")  # noqa: SLF001
    except ApiError as exc:
        return str(exc)
    return None


def print_workday_status(*, include_bundles: bool = True) -> None:
    """Render machine policy and every known local watcher."""
    prefs = load_preferences()
    roots, stale = _known_bundle_roots(
        prefs,
        include_current=True,
        prune=False,
    )
    bound = {root: _bundle_is_cloud_bound(root) for root in roots}
    running = [(root, is_running(root)) for root in roots]
    running_count = sum(bound[root] and record is not None for root, record in running)
    bound_count = sum(bound.values())
    unbound_count = len(roots) - bound_count
    is_on = not prefs.prompt_stream_muted and not prefs.autostart_disabled

    state = "ON" if is_on else "OFF"
    console.print(f"[sf.label]Spec[/] [bold]{state}[/]")
    dim(
        f"  {len(roots)} project{'s' if len(roots) != 1 else ''} known"
        f" · {running_count} watching"
        + (f" · {unbound_count} need connection" if unbound_count else "")
        + (f" · {bound_count - running_count} stopped" if bound_count > running_count else "")
    )
    if os.environ.get("SPEC_NO_AUTOSTART", "").strip() == "1":
        warn("SPEC_NO_AUTOSTART=1 is set in this shell; automatic starts are suppressed.")
    if stale:
        dim(f"  {stale} stale bundle registration(s) will be pruned by `spec on` or `spec off`.")

    if not include_bundles:
        return
    if not roots:
        dim("  No projects found. Run `spec discover` once.")
        return
    for root, record in running:
        if not bound[root]:
            dim(f"  Needs connection  {root}")
        elif record is None:
            dim(f"  Stopped           {root}")
        else:
            dim(f"  Watching          {root} · pid {record.pid}")


@click.command("on")
def workday_on_cmd() -> None:
    """Turn Spec on for this machine and start known bundle watchers.

    This is the start-of-workday command. It discovers bundles beneath saved
    workspace roots, connects any fresh bundle without uploading its contents,
    and idempotently starts each known watcher. Per-bundle
    ``cloud.prompt_stream`` policy remains authoritative.
    """
    prefs = load_preferences()
    prefs.prompt_stream = "default"
    prefs.autostart = "default"
    roots, stale = _known_bundle_roots(prefs, include_current=True, prune=True)
    prefs.save()

    creds = load_credentials()
    login_error = _cloud_login_error(creds)
    if login_error:
        ok("Spec is ON for this machine.")
        warn(f"{login_error}; run `spec login`, then `spec on` again.")
        print_workday_status()
        return

    started = 0
    already_running = 0
    connected = 0
    rules_refreshed = 0
    failures: list[tuple[Path, str]] = []
    missing_rules: list[Path] = []
    for root in roots:
        if not _bundle_is_cloud_bound(root):
            try:
                binding = ensure_cloud_binding(root, credentials=creds)
            except (CloudBindingError, ApiError) as exc:
                failures.append((root, str(exc)))
                continue
            if binding.changed_manifest:
                connected += 1
        if _missing_agent_rules(root):
            try:
                refresh_agent_rules(root)
                rules_refreshed += 1
            except OSError:
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
        f"({connected} connected, {started} started, "
        f"{already_running} already watching, {rules_refreshed} rules refreshed)."
    )
    if stale:
        dim(f"  pruned {stale} missing bundle registration(s).")
    for root, message in failures:
        warn(f"Could not connect or start {root}: {message}")
    if missing_rules:
        warn(
            f"{len(missing_rules)} project(s) could not refresh their managed agent rules."
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
    Cloud-side jobs are configured and run independently.
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
