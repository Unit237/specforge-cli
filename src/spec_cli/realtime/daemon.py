"""
Background-daemon lifecycle for ``spec watch``.

The product asks for ``spec watch`` to "flick on" the moment a user is
prompting inside a ``spec init``'d folder, without the user having to
think about it. That requires three things:

1. A way to run ``spec watch`` in the background, detached from the
   parent terminal, with a known PID we can check on or stop later.
2. A way for any ``spec`` invocation (or shell hook) to ask "is the
   watcher already running for this bundle?" cheaply (~1ms) so we
   don't spawn duplicates.
3. A way to stop it gracefully with a single user-friendly command.

This module is the substrate for all three. It is deliberately
**per-bundle**: the user's mental model is "I'm working in this
project; live broadcasting is on for *this* project". A shared
multi-bundle daemon is cleaner long-term but introduces IPC and
cross-bundle privacy questions we don't need to answer in v1.

The PID file lives at ``<bundle>/.spec/watch.pid`` (gitignored via the
existing ``.spec/`` block), so checking liveness is one ``stat`` + one
``kill(pid, 0)``. When the user `git clone`s a bundle to a new machine
the PID file isn't there, so the file actively encodes "is *this*
machine's watcher running" — exactly what we want.

Wire format of the PID file (one JSON object on disk; readable by
humans and by the CLI):

.. code-block:: json

    {
      "schema": 1,
      "pid": 12345,
      "started_at": "2026-05-08T20:31:14+00:00",
      "host": "my-laptop.local",
      "bundle_root": "/Users/me/Desktop/foo",
      "log_path": "/Users/me/Desktop/foo/.spec/watch.log",
      "spec_version": "0.2.7"
    }

Anything that doesn't parse → treated as missing (start a new daemon).
Anything whose ``pid`` isn't a live process for this user → treated as
missing (clean restart). The ``host`` field is informational; we never
try to control a process on another machine.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

# Where the PID file lives, relative to the bundle root. Gitignored
# under the umbrella ``.spec/`` rule; nothing else needs to know.
WATCH_PID_FILENAME = "watch.pid"
WATCH_LOG_FILENAME = "watch.log"
WATCH_DIR = ".spec"
WATCH_PID_SCHEMA_VERSION = 1

# How long ``stop()`` will wait for SIGTERM to land before escalating
# to SIGKILL. Generous enough that the watcher's finally-block (final
# clean-state broadcast, cursor save, consumer thread join) gets a
# fair chance to complete; tight enough that ``spec live stop`` feels
# like a foreground command.
DEFAULT_STOP_GRACE_SECS = 4.0

# How long ``start_in_background`` waits for the child to write
# ``watch.pid``. The child writes this immediately after resolving the
# bundle root (before cloud API calls); remaining slack covers cold
# Python startup on slow machines.
AUTOSTART_WAIT_SECS = 8.0


@dataclass(frozen=True)
class WatcherPidRecord:
    """One parsed PID file. Immutable so we can cache the lookup."""

    schema: int
    pid: int
    started_at: datetime | None
    host: str | None
    bundle_root: Path | None
    log_path: Path | None
    spec_version: str | None

    def is_local(self) -> bool:
        """True iff the recorded ``host`` matches this machine.

        We don't kill processes that don't belong to this host even
        if the PID happens to exist locally — that's a very different
        process. Cross-machine bundle clones are common (laptop +
        desktop on the same git remote) so being conservative here
        avoids 'spec live stop' nuking an unrelated PID.
        """
        if self.host is None:
            # Older records / hand-edited files: fall back to PID-only
            # check. Better than refusing to clean up a stale entry.
            return True
        try:
            local = socket.gethostname()
        except OSError:
            return True
        return self.host == local


# ── PID file primitives ────────────────────────────────────────────


def watch_dir(bundle_root: Path) -> Path:
    return bundle_root / WATCH_DIR


def watch_pid_path(bundle_root: Path) -> Path:
    return watch_dir(bundle_root) / WATCH_PID_FILENAME


def watch_log_path(bundle_root: Path) -> Path:
    return watch_dir(bundle_root) / WATCH_LOG_FILENAME


def write_pid_file(bundle_root: Path, *, pid: int, log_path: Path) -> Path:
    """Write the canonical PID file for ``bundle_root``.

    Atomic: renders to JSON in a sibling ``<file>.tmp`` and renames
    over the destination so a kill-9 mid-write can't leave a corrupt
    file the next ``ensure()`` would parse and act on.
    """
    spec_version = _detect_spec_version()
    payload: dict[str, object] = {
        "schema": WATCH_PID_SCHEMA_VERSION,
        "pid": int(pid),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "host": socket.gethostname(),
        "bundle_root": str(bundle_root.resolve()),
        "log_path": str(log_path.resolve()),
    }
    if spec_version:
        payload["spec_version"] = spec_version

    target = watch_pid_path(bundle_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)
    return target


def read_pid_file(bundle_root: Path) -> WatcherPidRecord | None:
    """Read + parse the PID file, returning ``None`` if missing or
    malformed. Callers that want liveness should use :func:`is_running`.
    """
    path = watch_pid_path(bundle_root)
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    schema = data.get("schema") if isinstance(data.get("schema"), int) else 0
    started_at = _parse_iso(data.get("started_at"))
    host = data.get("host") if isinstance(data.get("host"), str) else None
    bundle_field = data.get("bundle_root")
    bundle_path = Path(bundle_field) if isinstance(bundle_field, str) else None
    log_field = data.get("log_path")
    log_p = Path(log_field) if isinstance(log_field, str) else None
    spec_version = (
        data.get("spec_version")
        if isinstance(data.get("spec_version"), str)
        else None
    )
    return WatcherPidRecord(
        schema=schema,
        pid=pid,
        started_at=started_at,
        host=host,
        bundle_root=bundle_path,
        log_path=log_p,
        spec_version=spec_version,
    )


def remove_pid_file(bundle_root: Path) -> bool:
    """Delete the PID file. Returns True iff one was removed.
    Idempotent — silent on missing file."""
    path = watch_pid_path(bundle_root)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        log.debug("spec live: could not remove %s: %s", path, e)
        return False


# ── liveness ────────────────────────────────────────────────────────


def is_pid_alive(pid: int) -> bool:
    """Posix-portable "is this PID a process I can signal?" check.

    Two layers, in order:

    1. ``waitpid(pid, WNOHANG)`` — if the PID is one of *our* children
       (because we spawned it via ``start_in_background`` in the same
       interpreter, e.g. in tests), this opportunistically reaps any
       zombie state. Without this, a SIGTERM'd-but-not-waited child
       still answers ``kill(pid, 0)`` with success because the kernel
       hasn't released the PID yet — leading to false "still alive"
       reports inside the same process. ``ChildProcessError`` means
       "not my child" (or "already reaped") and we fall through.
    2. ``kill(pid, 0)`` — the standard "does this PID exist?" probe.

    ``EPERM`` means the PID is alive but owned by another user (we
    don't intend to fight that — we report it as "alive", which makes
    start refuse). ``ESRCH`` means dead.

    Windows note: ``os.kill`` with signal 0 raises on Windows. We
    don't support a long-running daemon on Windows in v1 — ``spec
    watch --background`` is documented as POSIX-only. The ``except``
    makes every other failure mode a "not alive" return so we don't
    crash.
    """
    if pid <= 0:
        return False
    try:
        result_pid, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        result_pid = 0
    except OSError:
        result_pid = 0
    if result_pid == pid:
        # Reaped a zombie — the process is dead.
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # PID exists, owned by someone else. Treat as alive — it's
        # safer to refuse a duplicate-start than to risk colliding
        # with another user's process.
        return True
    except OSError as e:
        if e.errno in (errno.ESRCH,):
            return False
        return False


def is_running(bundle_root: Path) -> WatcherPidRecord | None:
    """Return the live record if a watcher is running for this bundle
    on this host, else ``None``. Stale PID files are auto-cleared."""
    record = read_pid_file(bundle_root)
    if record is None:
        return None
    if not record.is_local():
        # Stale clone marker from a different machine — leave the
        # file alone (the other host owns it) but report "not
        # running here".
        return None
    if is_pid_alive(record.pid):
        return record
    # Local + dead → clean up so the next ensure() can spawn fresh.
    remove_pid_file(bundle_root)
    return None


# ── start / stop ────────────────────────────────────────────────────


@dataclass
class StartOutcome:
    """What ``start_in_background`` did. Used by callers to render
    user-facing messaging without parsing free text."""

    pid: int
    log_path: Path
    pid_path: Path
    already_running: bool


class WatcherStartError(RuntimeError):
    """Could not start the background watcher for a structural reason
    (no spec executable, no manifest, write failure). Distinct from
    "already running" which is a normal outcome."""


def start_in_background(
    bundle_root: Path,
    *,
    extra_args: Iterable[str] = (),
    spec_executable: Iterable[str] | None = None,
) -> StartOutcome:
    """Spawn ``spec watch`` for ``bundle_root`` as a detached process.

    ``extra_args`` is forwarded to the watch invocation (e.g.
    ``("--no-receive",)``). ``spec_executable`` overrides the default
    ``[sys.executable, "-m", "spec_cli"]`` lookup — used by tests to
    point at a stub script without going through the installed CLI.

    Idempotent: if a healthy daemon is already running for this
    bundle, returns its existing record with ``already_running=True``
    and does NOT spawn a duplicate.
    """
    bundle_root = bundle_root.resolve()
    existing = is_running(bundle_root)
    if existing is not None:
        return StartOutcome(
            pid=existing.pid,
            log_path=existing.log_path or watch_log_path(bundle_root),
            pid_path=watch_pid_path(bundle_root),
            already_running=True,
        )

    log_path = watch_log_path(bundle_root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Open append-mode so successive starts share one log file the
    # user can ``tail -f``. We let it grow unbounded for now; rotation
    # is a v0.3 concern.
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)

    cmd = list(spec_executable) if spec_executable is not None else _spec_exec_argv()
    cmd = [*cmd, "watch", "--background-runner", *extra_args]

    # ``start_new_session=True`` detaches from the controlling terminal
    # so closing the parent shell doesn't SIGHUP the daemon. ``stdin``
    # is ``DEVNULL`` so a daemon that accidentally tried to read would
    # get EOF instead of stalling on a closed pipe. ``close_fds=True``
    # keeps fds the parent had open from leaking into the child.
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(bundle_root),
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
            close_fds=True,
            env=_child_env(),
        )
    except (OSError, FileNotFoundError) as e:
        os.close(log_fd)
        raise WatcherStartError(
            f"Could not spawn `spec watch --background-runner`: {e}. "
            "If you installed spec via `uv tool install`, ensure `spec` is on PATH "
            "(`uv tool update-shell` and reopen your terminal)."
        ) from e
    finally:
        # The child has dup'd the fd; the parent doesn't need it.
        try:
            os.close(log_fd)
        except OSError:
            pass

    # The child writes the PID file once it has finished initialising
    # and entered the main loop. We poll for up to AUTOSTART_WAIT_SECS
    # so the caller can immediately render an accurate "started, pid
    # X" line. If the child crashes during init, the PID file never
    # appears and we report a structural error.
    deadline = time.monotonic() + AUTOSTART_WAIT_SECS
    while time.monotonic() < deadline:
        record = is_running(bundle_root)
        if record is not None:
            return StartOutcome(
                pid=record.pid,
                log_path=record.log_path or log_path,
                pid_path=watch_pid_path(bundle_root),
                already_running=False,
            )
        # Did the child die before writing the file?
        retcode = proc.poll()
        if retcode is not None:
            # Read the tail of the log so the error message is
            # actionable instead of "exited 1, see log".
            tail = _tail_log(log_path, max_bytes=2_000)
            raise WatcherStartError(
                f"`spec watch --background-runner` exited with code {retcode} "
                f"before writing a PID file.\n"
                f"  log: {log_path}\n"
                + (f"  tail:\n    {tail}" if tail else "")
            )
        time.sleep(0.05)

    # Timeout. The process is still alive but didn't write a PID
    # file — likely the spec executable is older than this CLI and
    # doesn't know about the ``--background-runner`` flag. Don't
    # leave the orphan running.
    try:
        proc.terminate()
    except OSError:
        pass
    raise WatcherStartError(
        "spec watch --background-runner did not write a PID file within "
        f"{AUTOSTART_WAIT_SECS:.1f}s. Common causes: an outdated `spec` on PATH "
        "(missing `--background-runner` — reinstall with "
        "`uv tool install --force git+https://github.com/Unit237/specforge-cli.git`), "
        "or an extremely slow shell/Python startup. Check `.spec/watch.log` in the bundle."
    )


@dataclass
class StopOutcome:
    """What ``stop()`` did so callers can render user feedback."""

    pid: int | None
    was_running: bool
    timed_out: bool  # SIGTERM did not finish in DEFAULT_STOP_GRACE_SECS
    killed: bool     # We sent SIGKILL after timeout


def stop(
    bundle_root: Path,
    *,
    grace: float = DEFAULT_STOP_GRACE_SECS,
) -> StopOutcome:
    """Stop the daemon for ``bundle_root``.

    Sends SIGTERM, waits up to ``grace`` seconds, then escalates to
    SIGKILL if the process is still alive. Removes the PID file on
    success. Returns ``was_running=False`` when there's nothing to
    stop — idempotent for shell-script use.
    """
    record = is_running(bundle_root)
    if record is None:
        # Make sure we leave no stale file behind even on no-op.
        remove_pid_file(bundle_root)
        return StopOutcome(pid=None, was_running=False, timed_out=False, killed=False)

    pid = record.pid
    try:
        import signal as _signal
        os.kill(pid, _signal.SIGTERM)
    except ProcessLookupError:
        remove_pid_file(bundle_root)
        return StopOutcome(pid=pid, was_running=False, timed_out=False, killed=False)
    except OSError as e:
        log.warning("spec live: kill(%s, SIGTERM) failed: %s", pid, e)
        return StopOutcome(pid=pid, was_running=True, timed_out=True, killed=False)

    deadline = time.monotonic() + max(0.5, grace)
    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            remove_pid_file(bundle_root)
            return StopOutcome(
                pid=pid, was_running=True, timed_out=False, killed=False
            )
        time.sleep(0.1)

    # Escalate. The watcher's finally block has had a fair chance.
    killed = False
    try:
        import signal as _signal
        os.kill(pid, _signal.SIGKILL)
        killed = True
    except (ProcessLookupError, OSError):
        pass

    # Tiny grace for the kernel to reap.
    for _ in range(20):
        if not is_pid_alive(pid):
            break
        time.sleep(0.05)

    remove_pid_file(bundle_root)
    return StopOutcome(pid=pid, was_running=True, timed_out=True, killed=killed)


# ── helpers ────────────────────────────────────────────────────────


def _spec_exec_argv() -> list[str]:
    """Best argv for re-invoking the *same* spec binary the user is
    running. Order of preference:

    1. ``$SPEC_BACKGROUND_EXEC`` — explicit env override (tests / power
       users who installed spec in a non-standard way).
    2. The current ``spec`` console script from ``sys.argv[0]``. This keeps a
       working CLI working even when a stale, broken ``spec`` appears earlier
       on PATH in a login shell.
    3. ``shutil.which("spec")`` — the installed CLI on PATH.
    4. ``[sys.executable, "-m", "spec_cli"]`` — fallback when spec is
       run via ``python -m`` and isn't on PATH (development checkouts,
       CI). Always works even when the entry-point script is missing.
    """
    override = os.environ.get("SPEC_BACKGROUND_EXEC")
    if override:
        # Honour shell-style splits so users can pass ``"uv run spec"``
        # etc. ``shlex.split`` returns a list ready for ``Popen``.
        import shlex

        parts = shlex.split(override)
        if parts:
            return parts

    invoked = Path(sys.argv[0]).expanduser()
    if invoked.name in ("spec", "spec.exe") and invoked.is_file():
        return [str(invoked.resolve())]

    import shutil

    found = shutil.which("spec")
    if found:
        return [found]
    return [sys.executable, "-m", "spec_cli"]


def _child_env() -> dict[str, str]:
    """Filter the parent's env for what the daemon should inherit.

    We strip ``SPEC_BACKGROUND_EXEC`` so the daemon can't fork another
    background watcher of itself in pathological cases, and we leave
    everything else (PATH, HOME, SPEC_API, SPEC_HOME, …) intact so
    the daemon resolves the same Cloud target the user did.
    """
    env = dict(os.environ)
    env.pop("SPEC_BACKGROUND_EXEC", None)
    # Mark child so it can disambiguate "I'm a background daemon" in
    # logs / error reports without parsing argv.
    env["SPEC_LIVE_BACKGROUND"] = "1"
    # Background daemons write to ``.spec/watch.log`` (not a TTY). Without
    # unbuffered stdio, Rich output can sit invisible for many seconds.
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _detect_spec_version() -> str | None:
    """Best-effort: read the installed package's version. Used as a
    diagnostic in the PID file so a user looking at it can tell which
    CLI version started the daemon."""
    try:
        from importlib.metadata import version

        return version("spec-cli")
    except Exception:  # noqa: BLE001
        return None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _tail_log(path: Path, *, max_bytes: int) -> str:
    """Last ~max_bytes of the log file, decoded best-effort. Used in
    error messages when the daemon dies during init so the user gets
    actionable context inline instead of a "see watch.log" pointer."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return ""
    try:
        return data.decode("utf-8", errors="replace").strip()
    except UnicodeDecodeError:
        return ""


__all__ = [
    "AUTOSTART_WAIT_SECS",
    "DEFAULT_STOP_GRACE_SECS",
    "StartOutcome",
    "StopOutcome",
    "WATCH_DIR",
    "WATCH_LOG_FILENAME",
    "WATCH_PID_FILENAME",
    "WATCH_PID_SCHEMA_VERSION",
    "WatcherPidRecord",
    "WatcherStartError",
    "is_pid_alive",
    "is_running",
    "read_pid_file",
    "remove_pid_file",
    "start_in_background",
    "stop",
    "watch_dir",
    "watch_log_path",
    "watch_pid_path",
    "write_pid_file",
]
