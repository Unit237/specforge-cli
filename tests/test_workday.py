"""Machine-wide ``spec on`` / ``spec off`` workday controls."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from spec_cli.cli import cli
from spec_cli.preferences import Preferences, load_preferences, remember_bundle
from spec_cli.realtime import StartOutcome, StopOutcome
from spec_cli.realtime.active_edits import ActiveEditsStore


def _bundle(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "spec.yaml").write_text("name: demo\n", encoding="utf-8")
    (root / "AGENTS.md").write_text(
        "<!-- >>> spec live coordination >>>\n",
        encoding="utf-8",
    )
    (root / ".cursor" / "rules").mkdir(parents=True)
    (root / ".cursor" / "rules" / "spec-team-presence.mdc").write_text(
        "rules\n",
        encoding="utf-8",
    )
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    return root


def _named_bundle(parent: Path, name: str) -> Path:
    root = parent / name
    root.mkdir()
    (root / "spec.yaml").write_text(
        f'schema: "spec/v0.1"\nname: {name}\n',
        encoding="utf-8",
    )
    return root


def test_remember_bundle_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    root = _bundle(tmp_path)

    remember_bundle(root)
    remember_bundle(root)

    assert load_preferences().bundles == [str(root.resolve())]


def test_spec_on_enables_preferences_registers_current_and_starts(
    tmp_path,
    monkeypatch,
):
    spec_home = tmp_path / "spec-home"
    monkeypatch.setenv("SPEC_HOME", str(spec_home))
    root = _bundle(tmp_path)
    monkeypatch.chdir(root)
    Preferences(prompt_stream="muted", autostart="off").save()

    started: list[Path] = []

    def fake_start(bundle_root: Path) -> StartOutcome:
        started.append(bundle_root)
        return StartOutcome(
            pid=123,
            log_path=bundle_root / ".spec" / "watch.log",
            pid_path=bundle_root / ".spec" / "watch.pid",
            already_running=False,
        )

    monkeypatch.setattr(
        "spec_cli.commands.workday.load_credentials",
        lambda: SimpleNamespace(access_token="token"),
    )
    monkeypatch.setattr("spec_cli.commands.workday.start_in_background", fake_start)

    result = CliRunner().invoke(cli, ["on"])

    assert result.exit_code == 0, result.output
    prefs = load_preferences()
    assert prefs.prompt_stream == "default"
    assert prefs.autostart == "default"
    assert prefs.bundles == [str(root.resolve())]
    assert started == [root.resolve()]
    assert "Spec is ON" in result.output


def test_spec_on_from_workspace_registers_all_peer_bundles(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    first = _named_bundle(tmp_path, "alpha")
    second = _named_bundle(tmp_path, "beta")
    monkeypatch.chdir(tmp_path)
    started: list[Path] = []

    monkeypatch.setattr(
        "spec_cli.commands.workday.load_credentials",
        lambda: SimpleNamespace(access_token="token"),
    )
    monkeypatch.setattr(
        "spec_cli.commands.workday.start_in_background",
        lambda root: (
            started.append(root)
            or StartOutcome(
                pid=123,
                log_path=root / ".spec" / "watch.log",
                pid_path=root / ".spec" / "watch.pid",
                already_running=False,
            )
        ),
    )

    result = CliRunner().invoke(cli, ["on"])

    assert result.exit_code == 0, result.output
    assert started == [first.resolve(), second.resolve()]
    assert load_preferences().bundles == [str(first.resolve()), str(second.resolve())]


def test_spec_off_disables_preferences_stops_all_and_releases_locks(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    root = _bundle(tmp_path)
    monkeypatch.chdir(root)
    Preferences(bundles=[str(root)]).save()
    store = ActiveEditsStore(root)
    store.acquire(["src/app.py"], agent="codex", session_id="thread-1")

    stopped: list[Path] = []

    def fake_stop(bundle_root: Path) -> StopOutcome:
        stopped.append(bundle_root)
        return StopOutcome(
            pid=456,
            was_running=True,
            timed_out=False,
            killed=False,
        )

    monkeypatch.setattr("spec_cli.commands.workday.stop_daemon", fake_stop)

    result = CliRunner().invoke(cli, ["off"])

    assert result.exit_code == 0, result.output
    prefs = load_preferences()
    assert prefs.prompt_stream == "muted"
    assert prefs.autostart == "off"
    assert stopped == [root.resolve()]
    assert store.list() == []
    assert "1 stopped" in result.output
    assert "1 locks released" in result.output


def test_spec_status_works_outside_a_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    monkeypatch.chdir(tmp_path)
    Preferences(prompt_stream="muted", autostart="off").save()

    result = CliRunner().invoke(cli, ["status"])

    assert result.exit_code == 0, result.output
    assert "Spec workday" in result.output
    assert "OFF" in result.output
    assert "machine status only" in result.output
