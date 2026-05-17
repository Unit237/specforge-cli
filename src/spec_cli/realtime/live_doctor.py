"""
Health checks for the Spec Live background watcher.

Used by ``spec live doctor`` and by the watcher when no prompt has been
broadcast successfully for a while (see ``QUIET_PROMPT_POST_SECS``).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..config import find_bundle_root, load_manifest
from ..preferences import load_preferences
from ..stage import historical_bundle_paths
from .daemon import is_pid_alive, is_running, read_pid_file, watch_log_path
from .tracker import CURSOR_FILENAME

# Warn when the watch log has not been written for this long while the
# daemon is supposedly running (producer loop should touch it regularly).
DEFAULT_LOG_STALE_SECS = 3600

# After this many seconds without a successful prompt POST, run doctor
# from inside the watcher (at most once per interval).
QUIET_PROMPT_POST_SECS = 300


Severity = Literal["info", "warn", "error"]


@dataclass(frozen=True)
class LiveDoctorFinding:
    code: str
    severity: Severity
    summary: str
    detail: str = ""
    fix: str = ""


def diagnose_live_health(
    bundle_root: Path,
    *,
    expect_broadcasting: bool = True,
    log_stale_secs: float = DEFAULT_LOG_STALE_SECS,
    now: float | None = None,
) -> list[LiveDoctorFinding]:
    """Return actionable findings for ``bundle_root`` (resolved)."""
    root = bundle_root.resolve()
    findings: list[LiveDoctorFinding] = []
    clock = time.time() if now is None else now

    prefs = load_preferences()
    try:
        manifest = load_manifest(root)
        bundle_enabled = manifest.prompt_stream_enabled
    except Exception:  # noqa: BLE001
        bundle_enabled = True

    resolved_broadcast = bundle_enabled and not prefs.prompt_stream_muted
    if expect_broadcasting and not resolved_broadcast:
        findings.append(
            LiveDoctorFinding(
                code="broadcast_off",
                severity="info",
                summary="Broadcasting is off for this bundle or machine.",
                fix="Run `spec live on` and `spec live unmute`, then `spec live restart`.",
            )
        )
        return findings

    record = is_running(root)
    pid_record = read_pid_file(root)

    if expect_broadcasting and record is None:
        findings.append(
            LiveDoctorFinding(
                code="daemon_not_running",
                severity="warn",
                summary="Spec Live daemon is not running for this bundle.",
                fix="Run `spec live start` from the bundle directory (or a parent).",
            )
        )
    elif pid_record is not None and record is None:
        findings.append(
            LiveDoctorFinding(
                code="daemon_stale_pid",
                severity="warn",
                summary="watch.pid exists but the process is not alive.",
                detail=f"Recorded pid {pid_record.pid}.",
                fix="Run `spec live start` to spawn a fresh watcher.",
            )
        )

    active = record or pid_record
    if active is not None and active.bundle_root is not None:
        recorded = active.bundle_root.resolve()
        if recorded != root:
            findings.append(
                LiveDoctorFinding(
                    code="bundle_root_mismatch",
                    severity="error",
                    summary="watch.pid bundle_root does not match this bundle.",
                    detail=f"pid file: {recorded}\nresolved:  {root}",
                    fix=(
                        "Run `spec live stop`, then `cd` to the bundle and "
                        "`spec live start`. Remove stray state under the old "
                        "path if you moved the repo."
                    ),
                )
            )

    log_path = (
        (active.log_path if active and active.log_path else None)
        or watch_log_path(root)
    )
    if active is not None and record is not None:
        if not log_path.is_file():
            findings.append(
                LiveDoctorFinding(
                    code="log_missing",
                    severity="warn",
                    summary="Watcher log file is missing.",
                    detail=str(log_path),
                    fix="Run `spec live restart` from the bundle directory.",
                )
            )
        else:
            try:
                age = clock - log_path.stat().st_mtime
            except OSError:
                age = None
            if age is not None and age > log_stale_secs:
                findings.append(
                    LiveDoctorFinding(
                        code="log_stale",
                        severity="warn",
                        summary=(
                            "Watcher log has not been updated recently "
                            f"({int(age // 60)} min ago)."
                        ),
                        detail=str(log_path),
                        fix=(
                            "The process may be stuck. Run "
                            "`spec live doctor`, then `spec live restart`."
                        ),
                    )
                )

    # Stray live state under historical bundle paths (common after a move).
    for alt in historical_bundle_paths(root)[1:]:
        stray_cursor = alt / ".spec" / CURSOR_FILENAME
        if not stray_cursor.is_file():
            continue
        try:
            if clock - stray_cursor.stat().st_mtime < log_stale_secs:
                findings.append(
                    LiveDoctorFinding(
                        code="stray_live_state",
                        severity="warn",
                        summary="Live cursor state under an old bundle path.",
                        detail=str(stray_cursor),
                        fix=(
                            f"Run `spec live stop` and remove `{alt}/.spec/` "
                            f"or update SPEC_BUNDLE_ROOT to `{root}`."
                        ),
                    )
                )
        except OSError:
            continue

    # Light session mapping check (Cursor only — fastest signal).
    if expect_broadcasting and resolved_broadcast and record is not None:
        try:
            from ..sources.cursor import (
                CursorError,
                cursor_workspace_storage_root,
                read_cursor_sessions,
            )

            paths = historical_bundle_paths(root)
            if cursor_workspace_storage_root().exists():
                n_cursor = sum(
                    1 for _ in _limited_sessions(read_cursor_sessions(paths))
                )
                if n_cursor == 0:
                    findings.append(
                        LiveDoctorFinding(
                            code="no_cursor_sessions",
                            severity="warn",
                            summary=(
                                "No Cursor Composer / Agent sessions map to "
                                "this bundle."
                            ),
                            fix=(
                                "Open this repo (or a parent folder) in Cursor "
                                "Agent mode, not sidebar Chat only. Then "
                                "`spec live restart`."
                            ),
                        )
                    )
        except CursorError:
            pass

    if not findings and expect_broadcasting and resolved_broadcast:
        findings.append(
            LiveDoctorFinding(
                code="ok",
                severity="info",
                summary="Spec Live wiring looks healthy for this bundle.",
            )
        )

    return findings


def _limited_sessions(sessions, limit: int = 5):  # type: ignore[no-untyped-def]
    for i, s in enumerate(sessions):
        if i >= limit:
            break
        yield s


def emit_live_doctor_warnings(
    bundle_root: Path,
    *,
    expect_broadcasting: bool = True,
) -> int:
    """Log warn/dim lines for non-info findings. Returns warning count."""
    from ..ui import dim, warn

    count = 0
    for f in diagnose_live_health(bundle_root, expect_broadcasting=expect_broadcasting):
        if f.severity == "info":
            continue
        count += 1
        if f.severity == "error":
            warn(f"Spec Live doctor: {f.summary}")
        else:
            warn(f"Spec Live doctor: {f.summary}")
        if f.detail:
            for line in f.detail.splitlines():
                dim(f"  {line}")
        if f.fix:
            dim(f"  → {f.fix}")
    if count:
        dim(
            f"  (run `spec live doctor` from {bundle_root} for the full report)"
        )
    return count


def resolve_bundle_root_for_doctor(start: Path | None = None) -> Path:
    """Bundle root for doctor commands — raises ``BundleNotFoundError``."""
    return find_bundle_root(start)
