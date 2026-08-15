"""
``spec watch`` — Spec Live daemon (broadcast + receive workspace prompts).

Foreground long-running command; ^C / SIGTERM stop it cleanly. Designed
to be run in a dedicated terminal pane alongside an editor — output is
quiet, single-line in compact mode, multi-line in default mode.

Wires ``spec_cli.realtime.run_watcher`` into the `click` surface and
resolves the cloud target the same way ``spec push`` does (manifest
``cloud.project`` → CLI flag → URL override). On a project that hasn't
been pushed yet this command will refuse — there's nothing to watch
until the bundle exists in Cloud.
"""
from __future__ import annotations

import os

import click

from ..api import ApiError, CloudClient
from ..config import (
    BundleNotFoundError,
    RemoteUrlError,
    find_bundle_root,
    load_credentials,
    load_manifest,
    parse_cloud_project,
)
from ..preferences import load_preferences, remember_bundle
from ..realtime import (
    WatcherOptions,
    WatcherStartError,
    is_running,
    remove_pid_file,
    run_watcher,
    start_in_background,
    watch_log_path,
    write_pid_file,
)
from ..ui import dim, fatal, info, ok, warn


@click.command("watch")
@click.option(
    "--no-broadcast",
    is_flag=True,
    help="Receive only — do not POST any of your local prompts to the team feed.",
)
@click.option(
    "--no-receive",
    is_flag=True,
    help="Broadcast only — do not display teammates' incoming prompts.",
)
@click.option(
    "--no-bootstrap",
    is_flag=True,
    help=(
        "Do not print recent Cloud history on connect — only new SSE "
        "events (and any gap replay since your last run)."
    ),
)
@click.option(
    "--mirror",
    is_flag=True,
    help=(
        "Append every incoming peer turn to "
        "`prompts/captured/peers/<handle>/<branch>.prompts`. Local "
        "cache only — never pushed; gitignored by default."
    ),
)
@click.option(
    "--verbose-out",
    is_flag=True,
    help=(
        "Broadcast assistant *full text* in addition to summaries. Off "
        "by default — assistant bodies are big and often sensitive. "
        "Equivalent to setting `cloud.prompt_stream.verbose: true` in "
        "spec.yaml for this run only."
    ),
)
@click.option(
    "--compact",
    is_flag=True,
    help="One line per event instead of the multi-line default.",
)
@click.option(
    "--show-tool-runs/--no-tool-runs",
    "show_tool_runs",
    default=False,
    show_default=True,
    help=(
        "Expand each incoming assistant turn's structured ``tool_calls`` "
        "list under the prose body (``Edit auth.py``, ``Bash \"pytest -q\"``, "
        "``Read main.py``…), and keep fenced code blocks in the prose "
        "intact instead of collapsing them to ``[code: lang ~N lines]``. "
        "Off by default — the default pane shows full AI narration "
        "without code so concurrent threads stay scannable. The auto-critic "
        "still inspects every tool call regardless of this flag."
    ),
)
@click.option(
    "--poll",
    "poll_interval",
    type=float,
    default=None,
    help="Seconds between local-transcript scans (default 2.0).",
)
@click.option(
    "--branch-only",
    "branch_only",
    is_flag=True,
    help=(
        "Only broadcast turns whose branch matches the current git "
        "branch. Off by default — quiet branches still benefit from "
        "showing peer activity, but if you're worried about noise on "
        "branch switches this is the kill-switch."
    ),
)
@click.option(
    "--project",
    "-p",
    default=None,
    help=(
        "Override `cloud.project` from spec.yaml. Accepts `<handle>/<slug>` "
        "or a bare slug (uses your handle from saved credentials)."
    ),
)
@click.option(
    "--background",
    is_flag=True,
    help=(
        "Detach into the background and return immediately. The daemon "
        "writes its PID to `.spec/watch.pid` and its output to "
        "`.spec/watch.log`. Stop with `spec live stop`. Idempotent — "
        "if a daemon is already running for this bundle, prints its "
        "PID and exits 0 without spawning a duplicate."
    ),
)
@click.option(
    "--background-runner",
    "background_runner",
    is_flag=True,
    hidden=True,
    help=(
        "Internal flag used by the autostart machinery — never set this "
        "by hand. Marks the current process as the spawned daemon and "
        "writes `.spec/watch.pid` before cloud init so the parent can "
        "confirm startup quickly."
    ),
)
def watch_cmd(
    no_broadcast: bool,
    no_receive: bool,
    no_bootstrap: bool,
    mirror: bool,
    verbose_out: bool,
    compact: bool,
    show_tool_runs: bool,
    poll_interval: float | None,
    branch_only: bool,
    project: str | None,
    background: bool,
    background_runner: bool,
) -> None:
    """Stream visible workspace prompts in real time.

    For each new turn in any local Cursor / Codex / Claude Code / Compress session,
    Spec Live POSTs a redacted event to Spec Cloud. Cloud fans the
    event out over a workspace SSE stream. Your foreground watcher renders
    every conversation you author; accepted teammates render every conversation
    authored by one another, all within a few seconds.

    Broadcasting follows ``cloud.prompt_stream`` in spec.yaml (default ON).
    Use ``spec live off`` / ``spec live mute`` to opt out. Receiving teammate
    events is always available under the Cloud workspace visibility rule. Use `--mirror`
    to also drop incoming peer events into a local file you can grep.

    By default this runs in the foreground; pass `--background` to
    detach into a background daemon you can manage with `spec live
    start/stop/status`.
    """
    if no_broadcast and no_receive:
        fatal(
            "Both --no-broadcast and --no-receive set — nothing to do. "
            "Drop one of the flags."
        )
        return
    if background and background_runner:
        # Defensive: nothing prevents a curious user from passing both,
        # but the combination is meaningless — ``--background`` spawns
        # a child that *itself* runs ``--background-runner``.
        fatal("--background and --background-runner are mutually exclusive.")
        return

    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    remember_bundle(root)

    if background:
        # User asked us to fork+detach. Do that and return — the child
        # carries the actual `run_watcher` call.
        try:
            outcome = start_in_background(root)
        except WatcherStartError as e:
            fatal(str(e))
            return
        if outcome.already_running:
            info(
                f"spec live: already running for this bundle (pid {outcome.pid})."
            )
            dim(f"  log: {outcome.log_path}")
            dim("  stop with `spec live stop`")
        else:
            ok(
                f"spec live: started in background (pid {outcome.pid})."
            )
            dim(f"  log: {outcome.log_path}")
            dim("  stop with `spec live stop` · status: `spec live status`")
        return

    # For the spawned daemon, claim the PID file *before* manifest/API
    # work. ``start_in_background`` polls for this file — if we only
    # wrote it after ``resolve_project``, slow networks would exceed the
    # wait window and the parent would SIGTERM a healthy child.
    pid_owned = False
    if background_runner:
        existing = is_running(root)
        if existing is not None:
            sys_msg = (
                f"spec live: another daemon is already running for this bundle "
                f"(pid {existing.pid}); refusing to start a duplicate.\n"
            )
            click.echo(sys_msg, err=True)
            raise SystemExit(0)
        log_p = watch_log_path(root)
        try:
            write_pid_file(root, pid=os.getpid(), log_path=log_p)
        except OSError as e:
            fatal(f"Could not write PID file at {log_p.parent}: {e}")
            return
        pid_owned = True

    try:
        manifest = load_manifest(root)
        creds = load_credentials()
        if not creds or not creds.access_token:
            fatal("Not signed in. Run `spec login` first.")
            return

        raw = project or manifest.cloud_project
        if not raw:
            fatal(
                "No cloud project configured. Add `cloud.project: <handle>/<slug>` "
                "to spec.yaml or pass --project <handle>/<slug>."
            )
            return
        try:
            handle, slug = parse_cloud_project(raw, default_handle=creds.user_handle)
        except RemoteUrlError as e:
            fatal(str(e))
            return

        try:
            client = CloudClient(creds)
        except ApiError as e:
            fatal(str(e))
            return
        try:
            project_info = client.resolve_project(handle, slug)
        except ApiError as e:
            fatal(
                f"Could not resolve project '{handle}/{slug}': {e}\n"
                f"  · Check your sign-in (`spec login`).\n"
                f"  · Or push the bundle first (`spec push`) so Cloud knows it."
            )
            return
        project_id = int(project_info["id"])

        # Self-id + display block for echo suppression and the local
        # team-presence mirror. Echo filtering matches ``author.user_id``
        # **and** ``broadcast_client_id`` from the wire so the same
        # account on a second computer is not suppressed as a local echo.
        # If the server doesn't return ``user_id`` from ``/api/auth/me``
        # (older deploys), echo suppression silently no-ops on the user
        # dimension — the client id still distinguishes installs when the
        # field is present on events.
        self_user_id: int | None = None
        self_handle: str | None = creds.user_handle
        self_name: str | None = None
        try:
            me = client._request("GET", "/api/auth/me")  # noqa: SLF001
            if isinstance(me, dict):
                if isinstance(me.get("id"), int):
                    self_user_id = int(me["id"])
                handle_val = me.get("handle")
                if isinstance(handle_val, str) and handle_val:
                    self_handle = handle_val
                name_val = me.get("name")
                if isinstance(name_val, str) and name_val:
                    self_name = name_val
        except ApiError as e:
            warn(f"could not read /api/auth/me ({e}) — echo suppression off")

        # Broadcasting resolution. Default is ON; two opt-out layers:
        #   * --no-broadcast on the command line (this run only)
        #   * the bundle manifest can disable for the whole team
        #     (`spec live off` writes ``cloud.prompt_stream: disabled``)
        #   * the user can mute on this machine for every bundle
        #     (`spec live mute` writes ``~/.spec/preferences.json``)
        # All three are independent kill-switches; ANY of them off → no broadcast.
        prefs = load_preferences()
        broadcast_requested = not no_broadcast
        project_opted_in = manifest.prompt_stream_enabled
        user_muted = prefs.prompt_stream_muted
        broadcast_active = broadcast_requested and project_opted_in and not user_muted

        if broadcast_requested and not project_opted_in:
            warn(
                "broadcasting is disabled for this bundle — run `spec live on` "
                "(or set `cloud.prompt_stream: enabled` in spec.yaml) to share "
                "your prompts with the team. Running in receive-only mode."
            )
        elif broadcast_requested and user_muted:
            warn(
                "broadcasting is muted on this machine — run `spec live unmute` "
                "to share again. Receiving still works."
            )

        branch_filter = None
        if branch_only:
            from ..git import read_git_context

            git = read_git_context(root)
            branch_filter = git.branch
            if not branch_filter:
                warn("--branch-only set but no git branch detected; ignored.")

        project_label = f"{handle}/{slug}"
        opts = WatcherOptions(
            project_id=project_id,
            project_label=project_label,
            api_base=creds.api_base,
            access_token=creds.access_token,
            self_user_id=self_user_id,
            self_handle=self_handle,
            self_name=self_name,
            poll_interval=poll_interval if poll_interval and poll_interval > 0 else 2.0,
            broadcast=broadcast_active,
            receive=not no_receive,
            bootstrap_receive=not no_bootstrap and not no_receive,
            mirror=mirror,
            # Presence shares the same gates as prompt broadcasting so a
            # user who muted Spec Live doesn't unintentionally start
            # broadcasting their dirty file list. Receiving is always
            # available so the local mirror still gets populated.
            presence_enabled=broadcast_active,
            verbose_assistant=verbose_out or manifest.prompt_stream_verbose,
            compact_output=compact,
            show_tool_runs=show_tool_runs,
            project_branch_filter=branch_filter,
        )

        if mirror:
            dim("mirror enabled → prompts/captured/peers/")

        raise SystemExit(run_watcher(root, opts))
    finally:
        if pid_owned:
            remove_pid_file(root)
