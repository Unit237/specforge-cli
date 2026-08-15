"""Machine-wide ``spec on`` / ``spec off`` workday controls."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from spec_cli.api import ApiError
from spec_cli.cli import cli
from spec_cli.commands.workday import _cloud_login_error, _known_bundle_roots
from spec_cli.config import Credentials
from spec_cli.preferences import (
    Preferences,
    load_preferences,
    machine_broadcast_role,
    remember_bundle,
)
from spec_cli.realtime import StartOutcome, StopOutcome
from spec_cli.realtime.active_edits import ActiveEditsStore


def _bundle(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "spec.yaml").write_text(
        'schema: "spec/v0.1"\nname: demo\ncloud:\n'
        '  project: jon/demo\n  bundle_id: bdl_demo\n',
        encoding="utf-8",
    )
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
        f'schema: "spec/v0.1"\nname: {name}\ncloud:\n'
        f'  project: jon/{name}\n  bundle_id: bdl_{name}\n',
        encoding="utf-8",
    )
    return root


def test_remember_bundle_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    root = _bundle(tmp_path)

    remember_bundle(root)
    remember_bundle(root)

    assert load_preferences().bundles == [str(root.resolve())]


def test_preferences_ignore_disposable_codex_worktrees(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    stable = _named_bundle(tmp_path, "stable")
    transient_parent = tmp_path / ".codex-worktrees"
    transient_parent.mkdir()
    transient = _named_bundle(transient_parent, "task-123")
    Preferences(bundles=[str(transient), str(stable)]).save()

    assert load_preferences().bundles == [str(stable)]

    remember_bundle(transient)
    assert load_preferences().bundles == [str(stable)]


def test_workspace_rescan_ignores_disposable_codex_worktrees(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stable = _named_bundle(workspace, "stable")
    transient_parent = workspace / ".codex-worktrees"
    transient_parent.mkdir()
    _named_bundle(transient_parent, "task-123")
    prefs = Preferences(discovery_roots=[str(workspace)])

    roots, stale = _known_bundle_roots(prefs, include_current=False, prune=True)

    assert roots == [stable.resolve()]
    assert stale == 0
    assert load_preferences().bundles == [str(stable.resolve())]


def test_remember_bundle_fails_open_when_preferences_directory_is_read_only(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    root = _bundle(tmp_path)

    def deny_tempfile(*args, **kwargs):
        raise PermissionError("read-only preferences directory")

    monkeypatch.setattr("spec_cli.preferences.tempfile.mkstemp", deny_tempfile)

    # Bundle registration is housekeeping; it must never break init/watch.
    assert remember_bundle(root).bundles == [str(root.resolve())]
    assert not (tmp_path / "spec-home" / "preferences.json").exists()


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
    monkeypatch.setattr("spec_cli.commands.workday._cloud_login_error", lambda _c: None)
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
    monkeypatch.setattr("spec_cli.commands.workday._cloud_login_error", lambda _c: None)
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
    assert machine_broadcast_role(first) == "owner"
    assert machine_broadcast_role(second) == "member"


def test_spec_on_connects_and_starts_fresh_example(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    root = _named_bundle(tmp_path, "example")
    (root / "spec.yaml").write_text(
        'schema: "spec/v0.1"\nname: example\ncloud:\n  project: example\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        "spec_cli.commands.workday.load_credentials",
        lambda: SimpleNamespace(access_token="token"),
    )
    monkeypatch.setattr("spec_cli.commands.workday._cloud_login_error", lambda _c: None)
    connected: list[Path] = []
    monkeypatch.setattr(
        "spec_cli.commands.workday.ensure_cloud_binding",
        lambda bundle_root, credentials: (
            connected.append(bundle_root)
            or SimpleNamespace(changed_manifest=True)
        ),
    )
    started: list[Path] = []
    monkeypatch.setattr(
        "spec_cli.commands.workday.start_in_background",
        lambda bundle_root: (
            started.append(bundle_root)
            or StartOutcome(
                pid=123,
                log_path=bundle_root / ".spec" / "watch.log",
                pid_path=bundle_root / ".spec" / "watch.pid",
                already_running=False,
            )
        ),
    )

    result = CliRunner().invoke(cli, ["on"])

    assert result.exit_code == 0, result.output
    assert connected == [root.resolve()]
    assert started == [root.resolve()]
    assert "1 connected, 1 started" in result.output


def test_spec_on_preflights_expired_login_before_starting_watchers(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))
    root = _bundle(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        "spec_cli.commands.workday.load_credentials",
        lambda: SimpleNamespace(access_token="expired"),
    )
    monkeypatch.setattr(
        "spec_cli.commands.workday._cloud_login_error",
        lambda _c: "Session expired",
    )
    monkeypatch.setattr(
        "spec_cli.commands.workday.start_in_background",
        lambda _root: (_ for _ in ()).throw(AssertionError("must not start")),
    )

    result = CliRunner().invoke(cli, ["on"])

    assert result.exit_code == 0, result.output
    assert "Session expired" in result.output
    assert "run `spec login`, then `spec on` again" in result.output
    assert load_preferences().autostart == "default"


def test_spec_on_does_not_block_watchers_for_transient_cloud_failure(
    monkeypatch,
) -> None:
    class _UnavailableCloud:
        def __init__(self, _creds) -> None:
            pass

        def _request(self, *_args, **_kwargs):
            raise ApiError("502 Bad Gateway", status=502, transient=True)

    monkeypatch.setattr("spec_cli.commands.workday.CloudClient", _UnavailableCloud)
    creds = Credentials(api_base="https://spec.test", access_token="token")

    assert _cloud_login_error(creds) is None


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
    assert "Spec OFF" in result.output
    assert "OFF" in result.output
    assert "machine status only" in result.output
