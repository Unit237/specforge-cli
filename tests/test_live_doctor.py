"""``spec live doctor`` health checks."""

from __future__ import annotations

import json
import time
from pathlib import Path

import yaml

from spec_cli.realtime.daemon import write_pid_file, watch_log_path
from spec_cli.realtime.live_doctor import diagnose_live_health


def _bundle(tmp_path: Path, name: str = "b") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True)
    (root / "spec.yaml").write_text(
        yaml.safe_dump({"schema": "spec/v0.1", "name": name}),
        encoding="utf-8",
    )
    return root


def test_doctor_bundle_root_mismatch(tmp_path: Path, monkeypatch) -> None:
    from spec_cli.realtime.daemon import read_pid_file

    bundle = _bundle(tmp_path, "real")
    other = _bundle(tmp_path, "wrong")
    log = watch_log_path(bundle)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("ok\n", encoding="utf-8")
    write_pid_file(bundle, pid=99999, log_path=log)
    pid_path = bundle / ".spec" / "watch.pid"
    data = json.loads(pid_path.read_text(encoding="utf-8"))
    data["bundle_root"] = str(other.resolve())
    pid_path.write_text(json.dumps(data), encoding="utf-8")
    record = read_pid_file(bundle)
    assert record is not None
    monkeypatch.setattr(
        "spec_cli.realtime.live_doctor.is_running",
        lambda _root: record,
    )

    codes = {f.code for f in diagnose_live_health(bundle, now=time.time())}
    assert "bundle_root_mismatch" in codes


def test_doctor_stray_live_state(tmp_path: Path) -> None:
    from spec_cli.stage import load_index, save_index

    bundle = _bundle(tmp_path, "real")
    old = _bundle(tmp_path, "old")
    stray = old / ".spec"
    stray.mkdir(parents=True)
    (stray / "live-cursor.json").write_text("{}", encoding="utf-8")
    idx = load_index(bundle)
    idx.bundle_paths.append(str(old.resolve()))
    save_index(idx)

    codes = {f.code for f in diagnose_live_health(bundle, now=time.time())}
    assert "stray_live_state" in codes


def test_doctor_stale_log(tmp_path: Path, monkeypatch) -> None:
    from spec_cli.realtime.daemon import WatcherPidRecord, read_pid_file

    bundle = _bundle(tmp_path, "real")
    log = watch_log_path(bundle)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("old\n", encoding="utf-8")
    old = time.time() - 7200
    import os

    os.utime(log, (old, old))
    write_pid_file(bundle, pid=99999, log_path=log)
    record = read_pid_file(bundle)
    assert record is not None

    monkeypatch.setattr(
        "spec_cli.realtime.live_doctor.is_running",
        lambda _root: record,
    )

    codes = {f.code for f in diagnose_live_health(bundle, now=time.time())}
    assert "log_stale" in codes
