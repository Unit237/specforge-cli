"""Workspace scope and producer ownership for ``spec watch``."""

from __future__ import annotations

from types import SimpleNamespace

from click.testing import CliRunner

from spec_cli.api import ApiError
from spec_cli.cli import cli
from spec_cli.config import Credentials
from spec_cli.commands.watch import _resolve_watch_project


def test_background_project_resolution_waits_through_transient_failure(
    monkeypatch,
) -> None:
    client = SimpleNamespace()
    calls = iter(
        [
            ApiError("502", status=502, transient=True),
            ApiError("503", status=503, transient=True),
            {"id": 9},
        ]
    )

    def resolve_project(_handle, _slug):
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    client.resolve_project = resolve_project
    delays: list[float] = []
    monkeypatch.setattr("spec_cli.commands.watch.time.sleep", delays.append)

    assert _resolve_watch_project(
        client, "alice", "demo", keep_waiting=True
    ) == {"id": 9}
    assert delays == [5.0, 10.0]


def test_foreground_watch_reuses_background_producer(monkeypatch, tmp_path) -> None:
    root = tmp_path / "demo"
    root.mkdir()
    (root / "spec.yaml").write_text(
        'schema: "spec/v0.1"\nname: demo\ncloud:\n'
        '  project: alice/demo\n  bundle_id: bdl_demo\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec-home"))

    class _Cloud:
        def __init__(self, _creds) -> None:
            pass

        def resolve_project(self, _handle, _slug):
            return {"id": 7}

        def _request(self, *_args, **_kwargs):
            return {"id": 1, "handle": "alice", "name": "Alice"}

    captured = []
    monkeypatch.setattr(
        "spec_cli.commands.watch.load_credentials",
        lambda: Credentials(
            api_base="https://spec.test",
            access_token="token",
            user_handle="alice",
        ),
    )
    monkeypatch.setattr("spec_cli.commands.watch.CloudClient", _Cloud)
    monkeypatch.setattr(
        "spec_cli.commands.watch.is_running",
        lambda _root: SimpleNamespace(pid=4242),
    )
    monkeypatch.setattr(
        "spec_cli.commands.watch.run_watcher",
        lambda _root, opts: captured.append(opts) or 0,
    )

    result = CliRunner().invoke(cli, ["watch"])

    assert result.exit_code == 0, result.output
    assert "workspace feed across all bundles" in result.output
    assert len(captured) == 1
    assert captured[0].receive is True
    assert captured[0].render_received is True
    assert captured[0].broadcast is False
    assert captured[0].bootstrap_receive is False
    assert captured[0].persist_cursor is False

    captured.clear()
    result = CliRunner().invoke(cli, ["watch", "--bootstrap"])

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].bootstrap_receive is True
