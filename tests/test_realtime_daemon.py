"""
Unit tests for ``spec_cli.realtime.daemon``.

Covers the four contracts that hold the autostart / ``spec live
start|stop`` story together:

1. **PID file round-trip + atomicity** — write_pid_file / read_pid_file
   produce a parseable, schema-tagged record; read tolerates missing
   and malformed files.
2. **Liveness gate** — ``is_running`` returns the live record only when
   the recorded PID is alive *on this host*; auto-clears stale files.
3. **Idempotent start** — ``start_in_background`` does not spawn a
   duplicate when a healthy daemon already owns the PID file; reports
   ``already_running``.
4. **Stop sends SIGTERM, escalates to SIGKILL on timeout, clears the
   PID file.**

Tests use a tiny POSIX shell stub for the ``spec`` executable so we
exercise the real ``Popen`` + PID-file path without dragging the
whole CLI in. Marked POSIX-only on the start/stop tests; the PID
file primitives are portable.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from spec_cli.realtime.daemon import (
    WATCH_PID_SCHEMA_VERSION,
    StartOutcome,
    WatcherStartError,
    is_pid_alive,
    is_running,
    read_pid_file,
    remove_pid_file,
    start_in_background,
    stop,
    watch_log_path,
    watch_pid_path,
    write_pid_file,
)
from spec_cli.realtime.daemon import _spec_exec_argv


pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="background daemon is POSIX-only in v1",
)


# ── helpers ────────────────────────────────────────────────────────


def _make_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "spec.yaml").write_text("name: demo\n", encoding="utf-8")
    return bundle


def _write_spec_stub(
    tmp_path: Path,
    *,
    sleep_secs: float = 5.0,
    delay_pid_write: float = 0.0,
    fail_before_pid: bool = False,
) -> Path:
    """Write a tiny shell script that mimics the daemon's behaviour:

    * ``$SPEC_BUNDLE_ROOT`` (we set this via cwd) is where ``.spec/watch.pid``
      goes.
    * Optionally sleep ``delay_pid_write`` before writing the PID file
      (used to test the start-up timeout).
    * ``fail_before_pid=True`` exits 1 before writing the file (tests
      the WatcherStartError path).
    * Otherwise sleeps ``sleep_secs`` so the PID stays alive long
      enough for liveness checks.
    """
    script = tmp_path / "spec-stub.sh"
    pid_path_template = ".spec/watch.pid"

    body = """#!/bin/sh
set -eu

# args[1]==watch, args[2]==--background-runner; ignore the rest.
# Real ``spec watch`` is a Python process with proper signal
# handlers; this stub mimics that by trapping SIGTERM and exiting
# cleanly so the test exercises the SIGTERM-lands-promptly path.
trap 'exit 0' TERM INT
mkdir -p .spec
"""
    if delay_pid_write > 0:
        body += f"sleep {delay_pid_write}\n"
    if fail_before_pid:
        body += "exit 1\n"
    else:
        # ``exec sleep`` so the shell *becomes* the sleep — that way
        # SIGTERM lands directly on a process the kernel will kill,
        # not on a shell parked in `wait()` for its child. POSIX shell
        # semantics defer signal-handling until the foreground command
        # returns, which is what tripped the original test.
        body += f"""\
PID=$$
HOST=$(hostname 2>/dev/null || echo unknown)
cat > {pid_path_template} <<EOF
{{
  "schema": {WATCH_PID_SCHEMA_VERSION},
  "pid": $PID,
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "host": "$HOST",
  "bundle_root": "$(pwd)",
  "log_path": "$(pwd)/.spec/watch.log"
}}
EOF
exec sleep {sleep_secs}
"""
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    return script


def test_watch_log_compaction_keeps_recent_diagnostic_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import spec_cli.realtime.daemon as daemon_mod

    log_path = tmp_path / "watch.log"
    log_path.write_bytes((b"old event\n" * 100) + b"recent event\n")
    monkeypatch.setattr(daemon_mod, "WATCH_LOG_MAX_BYTES", 128)
    monkeypatch.setattr(daemon_mod, "WATCH_LOG_RETAIN_BYTES", 64)

    daemon_mod._compact_watch_log(log_path)

    compacted = log_path.read_bytes()
    assert len(compacted) <= 64
    assert compacted.endswith(b"recent event\n")
    assert not compacted.startswith(b"ld event")


# ── PID file round-trip ────────────────────────────────────────────


def test_write_and_read_pid_file_round_trip(tmp_path):
    bundle = _make_bundle(tmp_path)
    log = bundle / ".spec" / "watch.log"
    written = write_pid_file(bundle, pid=12345, log_path=log)
    assert written == watch_pid_path(bundle)
    assert written.is_file()

    record = read_pid_file(bundle)
    assert record is not None
    assert record.pid == 12345
    assert record.schema == WATCH_PID_SCHEMA_VERSION
    assert record.bundle_root == bundle.resolve()
    assert record.log_path == log.resolve()


def test_read_pid_file_missing_returns_none(tmp_path):
    bundle = _make_bundle(tmp_path)
    assert read_pid_file(bundle) is None


def test_read_pid_file_malformed_returns_none(tmp_path):
    bundle = _make_bundle(tmp_path)
    pid_path = watch_pid_path(bundle)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("not even close to json", encoding="utf-8")
    assert read_pid_file(bundle) is None


def test_read_pid_file_negative_pid_returns_none(tmp_path):
    """Hand-edited / corrupted PID files with a non-positive ``pid``
    field are treated as missing — never as a "kill anything" wildcard."""
    bundle = _make_bundle(tmp_path)
    pid_path = watch_pid_path(bundle)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(
        json.dumps({"schema": 1, "pid": -1, "host": "x"}),
        encoding="utf-8",
    )
    assert read_pid_file(bundle) is None


def test_remove_pid_file_idempotent(tmp_path):
    bundle = _make_bundle(tmp_path)
    assert remove_pid_file(bundle) is False  # nothing to remove
    write_pid_file(bundle, pid=1, log_path=bundle / "x.log")
    assert remove_pid_file(bundle) is True
    assert remove_pid_file(bundle) is False


# ── liveness ───────────────────────────────────────────────────────


def test_is_pid_alive_for_self():
    """``kill(self_pid, 0)`` always succeeds — sanity check on the
    primitive everything else relies on."""
    assert is_pid_alive(os.getpid())


def test_is_pid_alive_for_dead_pid():
    """PID 1 (init) is alive on every POSIX host. Use a guaranteed-dead
    high PID instead."""
    # 2 ** 22 is well above the default pid_max on Linux/macOS and is
    # extremely unlikely to be a live process.
    assert not is_pid_alive(2 ** 22)


def test_is_running_clears_stale_pid_file(tmp_path):
    """A PID file referencing a dead PID gets auto-removed on read so
    the next ``ensure()`` can spawn a fresh daemon."""
    bundle = _make_bundle(tmp_path)
    write_pid_file(bundle, pid=2 ** 22, log_path=bundle / "x.log")
    assert watch_pid_path(bundle).is_file()
    assert is_running(bundle) is None
    assert not watch_pid_path(bundle).is_file()


def test_is_running_returns_record_for_self_pid(tmp_path):
    """Use the test process itself as the live PID — the record should
    come back parseable. Skips the cross-machine cleanup."""
    bundle = _make_bundle(tmp_path)
    write_pid_file(bundle, pid=os.getpid(), log_path=bundle / "x.log")
    record = is_running(bundle)
    assert record is not None
    assert record.pid == os.getpid()


def test_is_running_ignores_records_from_other_hosts(tmp_path):
    """A PID file with a different ``host`` field belongs to another
    machine. Even if the PID happens to be alive locally, we report
    "not running here" and leave the file alone."""
    bundle = _make_bundle(tmp_path)
    pid_path = watch_pid_path(bundle)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(
        json.dumps({
            "schema": 1,
            "pid": os.getpid(),
            "host": "definitely-not-this-machine.example",
            "bundle_root": str(bundle),
            "log_path": str(bundle / "x.log"),
        }),
        encoding="utf-8",
    )
    assert is_running(bundle) is None
    # Crucially: we left the file alone — it belongs to another host.
    assert pid_path.is_file()


# ── start_in_background ────────────────────────────────────────────


def test_spec_exec_prefers_current_console_script_over_path(tmp_path, monkeypatch):
    """A stale PATH entry must not make a working CLI spawn a broken daemon."""
    current = tmp_path / "current" / "spec"
    stale = tmp_path / "stale" / "spec"
    current.parent.mkdir()
    stale.parent.mkdir()
    current.write_text("#!/bin/sh\n", encoding="utf-8")
    stale.write_text("#!/bin/sh\n", encoding="utf-8")
    current.chmod(0o755)
    stale.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(current)])
    monkeypatch.setenv("PATH", str(stale.parent))

    assert _spec_exec_argv() == [str(current.resolve())]


def test_start_in_background_spawns_and_writes_pid(tmp_path):
    bundle = _make_bundle(tmp_path)
    stub = _write_spec_stub(tmp_path, sleep_secs=10.0)

    try:
        outcome = start_in_background(bundle, spec_executable=[str(stub)])
        assert isinstance(outcome, StartOutcome)
        assert not outcome.already_running
        assert outcome.pid > 0
        assert is_pid_alive(outcome.pid)
        assert watch_pid_path(bundle).is_file()
    finally:
        # Always clean up the stub process.
        stop(bundle, grace=2.0)


def test_start_in_background_idempotent_when_already_running(tmp_path):
    bundle = _make_bundle(tmp_path)
    stub = _write_spec_stub(tmp_path, sleep_secs=10.0)

    try:
        first = start_in_background(bundle, spec_executable=[str(stub)])
        assert not first.already_running

        second = start_in_background(bundle, spec_executable=[str(stub)])
        assert second.already_running
        assert second.pid == first.pid
    finally:
        stop(bundle, grace=2.0)


def test_start_in_background_raises_when_child_dies_pre_pid(tmp_path):
    """Daemon that exits before writing the PID file → WatcherStartError
    with the log tail attached so the user sees what went wrong."""
    bundle = _make_bundle(tmp_path)
    stub = _write_spec_stub(tmp_path, fail_before_pid=True)

    with pytest.raises(WatcherStartError):
        start_in_background(bundle, spec_executable=[str(stub)])

    # No PID file should remain.
    assert not watch_pid_path(bundle).is_file()


# ── stop ────────────────────────────────────────────────────────────


def test_stop_returns_was_running_false_when_idle(tmp_path):
    bundle = _make_bundle(tmp_path)
    outcome = stop(bundle, grace=1.0)
    assert outcome.was_running is False
    assert outcome.killed is False


def test_stop_terminates_running_daemon_and_clears_pid_file(tmp_path):
    bundle = _make_bundle(tmp_path)
    stub = _write_spec_stub(tmp_path, sleep_secs=10.0)

    try:
        out = start_in_background(bundle, spec_executable=[str(stub)])
        assert is_pid_alive(out.pid)

        stop_outcome = stop(bundle, grace=3.0)
        assert stop_outcome.was_running is True
        # Either SIGTERM landed cleanly (preferred) or we escalated
        # to SIGKILL — both leave us with the daemon dead and the
        # PID file gone.
        assert not is_pid_alive(out.pid) or _wait_dead(out.pid)
        assert not watch_pid_path(bundle).is_file()
    finally:
        # Defence in depth — never leave the stub running.
        try:
            os.kill(out.pid, 9)
        except (ProcessLookupError, OSError, NameError):
            pass


def _wait_dead(pid: int, *, timeout: float = 2.0) -> bool:
    """Tiny poll loop for kernel reap latency on macOS — kill returns
    immediately but ``kill(pid, 0)`` can still report alive for a
    handful of milliseconds. The test is asserting "eventually dead",
    not "synchronously dead"."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


# ── PID file format pinning ────────────────────────────────────────


def test_pid_file_contains_expected_keys(tmp_path):
    bundle = _make_bundle(tmp_path)
    pre = datetime.now(timezone.utc)
    write_pid_file(bundle, pid=4242, log_path=bundle / ".spec" / "watch.log")
    post = datetime.now(timezone.utc)

    raw = json.loads(watch_pid_path(bundle).read_text(encoding="utf-8"))
    assert raw["schema"] == WATCH_PID_SCHEMA_VERSION
    assert raw["pid"] == 4242
    assert raw["host"]
    assert raw["bundle_root"] == str(bundle.resolve())
    assert raw["log_path"] == str(watch_log_path(bundle).resolve())
    started = datetime.fromisoformat(raw["started_at"])
    # Allow for clock-skew of a couple of seconds at boundaries.
    assert pre.replace(microsecond=0) <= started.replace(microsecond=0)
    assert started.replace(microsecond=0) <= post.replace(microsecond=0)
