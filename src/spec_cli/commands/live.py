"""
``spec live`` — control Spec Live (real-time prompt sharing) toggles.

Two layers of opt-out, both surfaced through this one command group:

* **Per-bundle** (``cloud.prompt_stream`` in ``spec.yaml``) — what the
  team agrees on. ``spec live on`` / ``spec live off`` flip this.
  Edited as plain YAML by anyone with the manifest open; this command
  is the friendly path that doesn't require remembering the key.
* **Per-user** (``~/.spec/preferences.json``) — what *you*, on this
  machine, want regardless of the bundle. ``spec live mute`` silences
  broadcasting on this laptop for every bundle; ``spec live unmute``
  removes the override. Useful for laptops with NDA work, demos, or
  side projects you don't want bleeding into the team feed.

``spec live status`` prints both layers and the resolved final state
("broadcasting: on/off") so the user can see why a setting is what it
is without opening two different files.

Spec Live broadcasting is **on by default** the moment the CLI is
installed. The opt-outs are the affordance; nothing else needs to
happen for new teammates to start sharing prompts.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import click

from ..config import (
    BundleNotFoundError,
    Manifest,
    dump_manifest,
    find_bundle_root,
    load_manifest,
)
from ..preferences import load_preferences
from ..realtime import (
    WatcherStartError,
    is_running,
    start_in_background,
    stop_daemon,
    watch_log_path,
    watch_pid_path,
)
from ..realtime.live_doctor import (
    LiveDoctorFinding,
    diagnose_live_health,
    resolve_bundle_root_for_doctor,
)
from ..ui import console, dim, fatal, info, ok, warn


def _try_load_manifest() -> Manifest | None:
    """Tolerant manifest load — returns ``None`` outside a bundle.

    The user-level commands (``mute`` / ``unmute`` / ``status``) work
    everywhere on the machine, so we don't fail when the user runs
    them outside a bundle root. The bundle-level ``on`` / ``off``
    commands re-check and bail loudly themselves.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError:
        return None
    try:
        return load_manifest(root)
    except Exception:  # noqa: BLE001 — best-effort
        return None


@click.group("live")
def live_group() -> None:
    """Spec Live — real-time prompt sharing toggles + daemon control.

    Broadcasting is on by default once the CLI is installed. The
    autostart hook in your shell rc starts ``spec watch`` in the
    background the first time you enter a bundle each session, so the
    live feed is on without you remembering to start it.

    \b
    Daemon (this bundle):
      spec live start         — start the background watcher
      spec live stop          — stop it
      spec live restart       — stop + start
      spec live status        — show daemon state + resolved settings
      spec live doctor        — diagnose watcher path / log / mapping issues

    \b
    Per-bundle policy (committed to spec.yaml):
      spec live on            — opt the bundle into broadcasting
      spec live off           — opt the bundle out

    \b
    Per-machine policy (~/.spec/preferences.json):
      spec live mute          — silence broadcasting on this laptop
      spec live unmute        — remove the per-machine mute
      spec live autostart on  — re-enable shell-hook autostart
      spec live autostart off — disable shell-hook autostart on this laptop

    \b
    Used by tooling (don't run by hand):
      spec live ensure        — fast idempotent start (used by shell hook)
    """


# ── per-bundle controls ──────────────────────────────────────────


@live_group.command("on")
@click.option(
    "--verbose",
    "verbose_flag",
    is_flag=True,
    help=(
        "Also enable verbose mode — broadcasts assistant *full text* "
        "(not just summaries). Off by default; assistant bodies are "
        "big and often sensitive."
    ),
)
def live_on_cmd(verbose_flag: bool) -> None:
    """Enable Spec Live broadcasting for this bundle.

    Writes ``cloud.prompt_stream: enabled`` to ``spec.yaml``. The
    setting is committed to git like any other manifest change, so
    teams agreeing on a policy do so visibly.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return
    manifest = load_manifest(root)
    manifest.set_cloud_prompt_stream(enabled=True, verbose=verbose_flag or None)
    dump_manifest(manifest)
    ok(
        "Spec Live ON for this bundle "
        + ("(verbose: assistant full text)" if verbose_flag else "(summary-only)")
    )
    dim("  written to spec.yaml — commit it so teammates inherit the setting.")


@live_group.command("off")
def live_off_cmd() -> None:
    """Disable Spec Live broadcasting for this bundle.

    Writes ``cloud.prompt_stream: disabled`` to ``spec.yaml``.
    Receivers — anyone running ``spec watch`` — still see incoming
    peer events; this only stops the bundle from broadcasting.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return
    manifest = load_manifest(root)
    manifest.set_cloud_prompt_stream(enabled=False)
    dump_manifest(manifest)
    ok("Spec Live OFF for this bundle")
    dim("  written to spec.yaml — commit it so teammates inherit the setting.")


# ── per-user controls ───────────────────────────────────────────


@live_group.command("mute")
def live_mute_cmd() -> None:
    """Silence Spec Live broadcasting on **this machine** for every bundle.

    The user-level kill-switch. Use this on a laptop with NDA / private
    work that you'd prefer not to mix into any team feed regardless of
    per-bundle settings. Receiving is unaffected.

    Stored in ``~/.spec/preferences.json``; revert with ``spec live unmute``.
    """
    prefs = load_preferences()
    if prefs.prompt_stream_muted:
        info("already muted")
        return
    prefs.prompt_stream = "muted"
    path = prefs.save()
    ok("Spec Live muted on this machine — broadcasting off for every bundle.")
    dim(f"  ({path})")
    dim("  receivers still see incoming peer events; this only stops your outgoing share.")


@live_group.command("unmute")
def live_unmute_cmd() -> None:
    """Remove the per-machine mute — defer to per-bundle settings again."""
    prefs = load_preferences()
    if not prefs.prompt_stream_muted:
        info("not muted — nothing to do")
        return
    prefs.prompt_stream = "default"
    prefs.save()
    ok("Spec Live unmuted — broadcasting follows each bundle's spec.yaml setting again.")


# ── autostart preference ───────────────────────────────────────


@live_group.group("autostart", invoke_without_command=False)
def live_autostart_group() -> None:
    """Control whether the shell hook auto-starts ``spec watch`` for you.

    The autostart hook fires when you enter a ``spec init``'d bundle
    in an interactive shell. It runs ``spec live ensure --quiet`` in
    the background, which spawns the watcher daemon iff one isn't
    already running. Disabling autostart suppresses the hook on this
    machine; you can still ``spec live start`` by hand.
    """


@live_autostart_group.command("on")
def live_autostart_on_cmd() -> None:
    """Re-enable shell-hook autostart of ``spec watch`` on this machine."""
    prefs = load_preferences()
    if not prefs.autostart_disabled:
        info("autostart: already on (default)")
        return
    prefs.autostart = "default"
    prefs.save()
    ok("autostart: ON — `spec live ensure` will spawn the daemon when you enter a bundle.")


@live_autostart_group.command("off")
def live_autostart_off_cmd() -> None:
    """Disable shell-hook autostart on this machine.

    The hook in your rc file still runs but is a no-op once
    ``autostart: off`` is set. Manual ``spec live start`` /
    ``spec watch --background`` still work.
    """
    prefs = load_preferences()
    if prefs.autostart_disabled:
        info("autostart: already off")
        return
    prefs.autostart = "off"
    path = prefs.save()
    ok("autostart: OFF on this machine.")
    dim(f"  ({path})")
    dim("  re-enable with `spec live autostart on`.")


# ── daemon lifecycle ────────────────────────────────────────────


@live_group.command("start")
def live_start_cmd() -> None:
    """Start ``spec watch`` in the background for this bundle.

    Equivalent to ``spec watch --background`` but reads more naturally
    in the daemon-control vocabulary. Idempotent — if a daemon is
    already running for the current bundle, this prints its PID and
    exits 0 without spawning a duplicate.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return
    try:
        outcome = start_in_background(root)
    except WatcherStartError as e:
        fatal(str(e))
        return
    if outcome.already_running:
        info(f"already running (pid {outcome.pid})")
    else:
        ok(f"spec live started (pid {outcome.pid})")
    dim(f"  log: {outcome.log_path}")
    dim("  stop with `spec live stop` · status with `spec live status`")


@live_group.command("stop")
@click.option(
    "--grace",
    type=float,
    default=None,
    help=(
        "Seconds to wait after SIGTERM before escalating to SIGKILL. "
        "Default 4s; the daemon's finally block (clean-state broadcast, "
        "cursor save) typically completes in well under a second."
    ),
)
def live_stop_cmd(grace: float | None) -> None:
    """Stop the background ``spec watch`` for this bundle.

    Sends SIGTERM, waits ``--grace`` seconds, then SIGKILL if the
    daemon is still alive. Reports what it did. Idempotent — calling
    on a bundle without a running daemon prints "not running" and
    exits 0.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return
    kwargs: dict = {}
    if grace is not None:
        kwargs["grace"] = grace
    outcome = stop_daemon(root, **kwargs)
    if not outcome.was_running:
        info("not running")
        return
    if outcome.killed:
        warn(
            f"daemon (pid {outcome.pid}) didn't exit within the grace period; "
            "sent SIGKILL."
        )
    elif outcome.timed_out:
        warn(
            f"daemon (pid {outcome.pid}) is still alive after the grace "
            "period — escalation failed."
        )
    else:
        ok(f"stopped (pid {outcome.pid})")


@live_group.command("restart")
@click.pass_context
def live_restart_cmd(ctx: click.Context) -> None:
    """Stop + start the background watcher for this bundle.

    Useful after upgrading the CLI (``uv tool install --force``) — the
    long-running daemon is still on the previous version until you
    bounce it.
    """
    ctx.invoke(live_stop_cmd, grace=None)
    ctx.invoke(live_start_cmd)


@live_group.command("ensure")
@click.option(
    "--quiet",
    is_flag=True,
    help=(
        "Print nothing on the happy path (already running OR successfully "
        "started). Used by the shell hook so an interactive prompt isn't "
        "polluted on every directory change."
    ),
)
def live_ensure_cmd(quiet: bool) -> None:
    """Idempotent: start the watcher if it isn't already running.

    Designed for tooling to call automatically — the shell hook fires
    this on every prompt-render, and the Claude Code UserPromptSubmit
    hook fires it before every prompt. Bails fast with exit 0 in any
    of these cases:

    * Not in a Spec bundle (no ``spec.yaml`` in or above ``$PWD``).
    * Daemon is already running for this bundle.
    * ``SPEC_NO_AUTOSTART`` is set in the environment.
    * The user disabled autostart with ``spec live autostart off``.
    * The user isn't signed in (``spec login`` is the prerequisite;
      we don't pop a login flow from a shell hook).

    Returns non-zero only on a structural failure that the user
    should know about — never on an opt-out, which is silent.
    """
    if os.environ.get("SPEC_NO_AUTOSTART", "").strip() == "1":
        if not quiet:
            dim("autostart skipped (SPEC_NO_AUTOSTART=1)")
        return

    prefs = load_preferences()
    if prefs.autostart_disabled:
        if not quiet:
            dim("autostart skipped (`spec live autostart off`)")
        return

    try:
        root = find_bundle_root()
    except BundleNotFoundError:
        # Not in a bundle — silent no-op. This is the common path on
        # any directory that isn't ``spec init``'d, including the
        # user's $HOME and most of /tmp.
        return

    if is_running(root) is not None:
        if not quiet:
            dim("already running")
        return

    # Don't kick off a daemon if the user hasn't logged in yet —
    # the daemon would just fail in resolve_project. Failing the
    # autostart silently here is friendlier than a stack trace in
    # ``.spec/watch.log`` no one reads.
    from ..config import load_credentials

    creds = load_credentials()
    if not creds or not creds.access_token:
        if not quiet:
            dim("autostart skipped (run `spec login` first)")
        return

    try:
        outcome = start_in_background(root)
    except WatcherStartError as e:
        # Surface only when not in quiet mode; the shell hook is quiet
        # so a flaky cold-start doesn't yell at the user on every cd.
        if not quiet:
            warn(f"autostart failed: {e}")
        return
    if quiet:
        return
    if outcome.already_running:
        dim(f"already running (pid {outcome.pid})")
    else:
        ok(f"spec live started in background (pid {outcome.pid})")


# ── inspection ──────────────────────────────────────────────────


@live_group.command("status")
def live_status_cmd() -> None:
    """Show the resolved Spec Live state for the current shell.

    Prints the per-bundle setting, the per-user mute / autostart, the
    daemon's PID + uptime if running, and the final resolved answer
    to "is broadcasting on right now?". The resolution rule is
    simple: per-user mute always wins; otherwise the per-bundle
    setting decides.
    """
    prefs = load_preferences()
    manifest = _try_load_manifest()

    console.print("[sf.label]Spec Live[/]")

    if manifest is None:
        dim("  bundle (spec.yaml):     (not in a Spec bundle)")
        bundle_enabled = None
        bundle_verbose = False
        bundle_root = None
    else:
        ps = manifest.prompt_stream
        bundle_enabled = bool(ps.get("enabled"))
        bundle_verbose = bool(ps.get("verbose"))
        state = "ON" if bundle_enabled else "OFF"
        verbose_tag = " · verbose: assistant full text" if bundle_verbose else " · summary-only"
        bundle_origin = "default" if not _has_explicit_prompt_stream(manifest) else "explicit"
        dim(
            f"  bundle (spec.yaml):     {state}{verbose_tag}  ({bundle_origin})"
        )
        bundle_root = manifest.root

    if prefs.prompt_stream_muted:
        dim("  machine (mute):         MUTED — overrides any bundle setting")
    else:
        dim("  machine (mute):         default — defers to bundle setting")

    if prefs.autostart_disabled:
        dim("  machine (autostart):    OFF — shell hook is a no-op")
    else:
        dim("  machine (autostart):    ON — daemon spawns on first cd into a bundle")

    if bundle_root is not None:
        record = is_running(bundle_root)
        if record is not None:
            uptime = _format_uptime(record.started_at)
            dim(
                f"  daemon:                 RUNNING (pid {record.pid}, "
                f"uptime {uptime})"
            )
            log_p = record.log_path or watch_log_path(bundle_root)
            dim(f"    log:                  {log_p}")
            dim(f"    pid file:             {watch_pid_path(bundle_root)}")
        else:
            dim("  daemon:                 not running")
            dim(
                "    start with:           spec live start  "
                "(or auto on next cd)"
            )

    if bundle_enabled is None:
        dim("  resolved broadcasting:  ?  (not in a bundle)")
        return

    resolved = bundle_enabled and not prefs.prompt_stream_muted
    if resolved:
        ok(f"  → broadcasting ON {'(verbose)' if bundle_verbose else ''}".rstrip())
    else:
        warn(
            "  → broadcasting OFF"
            + (" (machine mute)" if prefs.prompt_stream_muted else " (bundle off)")
        )
    dim("  receiving:              always available to project members.")


@live_group.command("doctor")
def live_doctor_cmd() -> None:
    """Diagnose Spec Live wiring for the bundle at this cwd.

    Checks ``watch.pid`` vs the resolved bundle root, log freshness,
    stray ``.spec`` state under old paths, and whether Cursor sessions
    map to this bundle. The background watcher runs the same checks
    automatically after ~5 minutes without a successful prompt POST.
    """
    try:
        root = resolve_bundle_root_for_doctor()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    findings = diagnose_live_health(root)
    console.print("[sf.label]Spec Live doctor[/]")
    dim(f"  bundle root:  {root.resolve()}")

    exit_code = 0
    for f in findings:
        _print_doctor_finding(f)
        if f.severity == "error":
            exit_code = 2
        elif f.severity == "warn" and exit_code == 0:
            exit_code = 1

    if exit_code:
        dim("  → Fix the items above, then run `spec live restart`.")
    raise SystemExit(exit_code)


def _print_doctor_finding(f: LiveDoctorFinding) -> None:
    if f.severity == "ok" or f.code == "ok":
        ok(f"  {f.summary}")
        return
    if f.severity == "error":
        warn(f"  {f.summary}")
    else:
        warn(f"  {f.summary}")
    if f.detail:
        for line in f.detail.splitlines():
            dim(f"    {line}")
    if f.fix:
        dim(f"    → {f.fix}")


def _has_explicit_prompt_stream(manifest: Manifest) -> bool:
    """Did ``cloud.prompt_stream`` appear in the manifest, or is the
    current state purely the default? Used by ``status`` to label the
    bundle line so users know whether the team has agreed on a policy
    or is just inheriting the default-on behaviour."""
    cloud = manifest.data.get("cloud") or {}
    if not isinstance(cloud, dict):
        return False
    return "prompt_stream" in cloud


def _format_uptime(started_at: datetime | None) -> str:
    """Best-effort uptime rendering for ``spec live status``. Returns
    ``"?"`` when the timestamp is missing/malformed; otherwise a
    compact ``1h23m`` / ``42m`` / ``17s`` string."""
    if started_at is None:
        return "?"
    try:
        now = datetime.now(timezone.utc)
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        delta = now - started_at
    except (TypeError, ValueError):
        return "?"
    secs = max(0, int(delta.total_seconds()))
    if secs < 60:
        return f"{secs}s"
    mins, s = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m{s:02d}s"
    hours, m = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h{m:02d}m"
    days, h = divmod(hours, 24)
    return f"{days}d{h:02d}h"
