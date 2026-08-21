"""Tests for the Spec Live opt-out layers.

Two layers, one resolution rule:

* ``cloud.prompt_stream`` in ``spec.yaml`` — per-bundle, defaults ON.
* ``~/.spec/preferences.json`` ``prompt_stream`` — per-user mute.

Resolved broadcast: ``bundle_enabled and not user_muted``. If either
is off, broadcasting is off. Receivers always work.

These tests pin the contract so we never regress on the "default on"
property — that's the whole acquisition story for Spec Live.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from spec_cli.config import Manifest, dump_manifest, load_manifest
from spec_cli.constants import MANIFEST_FILENAME
from spec_cli.preferences import Preferences, load_preferences


# ── manifest defaults ────────────────────────────────────────────


def _manifest_at(tmp_path: Path, body: dict) -> Manifest:
    p = tmp_path / MANIFEST_FILENAME
    p.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    return load_manifest(tmp_path)


def test_prompt_stream_default_is_on_when_unset(tmp_path):
    """Bedrock invariant: a bundle that has never heard of Spec Live
    still broadcasts. Removing this default is the kind of regression
    that silently kills the team-feed product.

    As of v0.4 the verbose default *also* flipped to True so
    ``spec team watch`` viewers see full assistant bodies without
    every teammate having to edit their ``spec.yaml``. Teams that
    want the old summary-only posture can set ``verbose: false``
    explicitly — see ``test_prompt_stream_explicit_verbose_false``.
    """
    m = _manifest_at(tmp_path, {"name": "demo"})
    assert m.prompt_stream_enabled is True
    assert m.prompt_stream_verbose is True


def test_prompt_stream_explicit_verbose_false(tmp_path):
    """Explicit opt-out wins over the new default. A team that
    flipped this off intentionally must keep their preference even
    after the default flip."""
    m = _manifest_at(
        tmp_path,
        {"cloud": {"prompt_stream": {"enabled": True, "verbose": False}}},
    )
    assert m.prompt_stream_enabled is True
    assert m.prompt_stream_verbose is False


def test_prompt_stream_default_is_on_when_cloud_block_present(tmp_path):
    m = _manifest_at(tmp_path, {"name": "demo", "cloud": {"project": "demo"}})
    assert m.prompt_stream_enabled is True


def test_prompt_stream_explicit_on(tmp_path):
    m = _manifest_at(
        tmp_path,
        {"cloud": {"prompt_stream": {"enabled": True, "verbose": True}}},
    )
    assert m.prompt_stream_enabled is True
    assert m.prompt_stream_verbose is True


def test_prompt_stream_explicit_off_via_string(tmp_path):
    m = _manifest_at(
        tmp_path, {"cloud": {"prompt_stream": "disabled"}}
    )
    assert m.prompt_stream_enabled is False


def test_prompt_stream_explicit_off_via_bool(tmp_path):
    m = _manifest_at(tmp_path, {"cloud": {"prompt_stream": False}})
    assert m.prompt_stream_enabled is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("on", True),
        ("ON", True),
        ("true", True),
        ("yes", True),
        ("enabled", True),
        ("off", False),
        ("OFF", False),
        ("disabled", False),
        ("no", False),
    ],
)
def test_prompt_stream_string_aliases(tmp_path, raw, expected):
    m = _manifest_at(tmp_path, {"cloud": {"prompt_stream": raw}})
    assert m.prompt_stream_enabled is expected


def test_set_cloud_prompt_stream_writes_explicit_mapping(tmp_path):
    m = _manifest_at(tmp_path, {"name": "demo"})
    m.set_cloud_prompt_stream(enabled=False)
    dump_manifest(m)
    reloaded = load_manifest(tmp_path)
    cloud = reloaded.data["cloud"]
    assert cloud["prompt_stream"] == {"enabled": False}
    assert reloaded.prompt_stream_enabled is False


def test_set_cloud_prompt_stream_with_verbose(tmp_path):
    m = _manifest_at(tmp_path, {"name": "demo"})
    m.set_cloud_prompt_stream(enabled=True, verbose=True)
    dump_manifest(m)
    reloaded = load_manifest(tmp_path)
    assert reloaded.prompt_stream_enabled is True
    assert reloaded.prompt_stream_verbose is True


# ── preferences (~/.spec) ───────────────────────────────────────


def test_preferences_default_is_unmuted(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path))
    prefs = load_preferences()
    assert prefs.prompt_stream == "default"
    assert prefs.prompt_stream_muted is False


def test_preferences_mute_persists_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path))
    prefs = Preferences()
    prefs.prompt_stream = "muted"
    prefs.save()

    fresh = load_preferences()
    assert fresh.prompt_stream == "muted"
    assert fresh.prompt_stream_muted is True


def test_preferences_unmute_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path))
    prefs = Preferences(prompt_stream="muted")
    prefs.save()
    assert load_preferences().prompt_stream_muted is True

    prefs2 = load_preferences()
    prefs2.prompt_stream = "default"
    prefs2.save()
    assert load_preferences().prompt_stream_muted is False


def test_preferences_malformed_file_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path))
    (tmp_path).mkdir(exist_ok=True)
    (tmp_path / "preferences.json").write_text("not json", encoding="utf-8")
    prefs = load_preferences()
    assert prefs.prompt_stream == "default"


def test_preferences_preserves_unknown_keys(tmp_path, monkeypatch):
    """Forward-compat: an older CLI must round-trip a newer CLI's
    settings without dropping them. We pin this so a future feature
    addition doesn't silently lose data on save."""
    monkeypatch.setenv("SPEC_HOME", str(tmp_path))
    (tmp_path).mkdir(exist_ok=True)
    (tmp_path / "preferences.json").write_text(
        '{"schema": 99, "future_feature": "yay", "prompt_stream": "muted"}',
        encoding="utf-8",
    )
    prefs = load_preferences()
    prefs.prompt_stream = "default"
    prefs.save()

    raw = (tmp_path / "preferences.json").read_text(encoding="utf-8")
    import json as _json

    data = _json.loads(raw)
    assert data["future_feature"] == "yay"
    assert data["prompt_stream"] == "default"


# ── resolution rule ─────────────────────────────────────────────


def test_resolution_rule_default_on(tmp_path, monkeypatch):
    """Fresh bundle, fresh user → broadcasting ON."""
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec_home"))
    m = _manifest_at(tmp_path, {"name": "demo"})
    prefs = load_preferences()
    assert m.prompt_stream_enabled and not prefs.prompt_stream_muted


def test_resolution_rule_bundle_off_kills_broadcast(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_HOME", str(tmp_path / "spec_home"))
    m = _manifest_at(tmp_path, {"cloud": {"prompt_stream": "off"}})
    prefs = load_preferences()
    resolved = m.prompt_stream_enabled and not prefs.prompt_stream_muted
    assert resolved is False


def test_resolution_rule_user_mute_kills_broadcast(tmp_path, monkeypatch):
    spec_home = tmp_path / "spec_home"
    monkeypatch.setenv("SPEC_HOME", str(spec_home))
    Preferences(prompt_stream="muted").save()
    m = _manifest_at(tmp_path, {"cloud": {"prompt_stream": "on"}})
    prefs = load_preferences()
    resolved = m.prompt_stream_enabled and not prefs.prompt_stream_muted
    assert resolved is False


def test_resolution_rule_any_off_kills_broadcast(tmp_path, monkeypatch):
    spec_home = tmp_path / "spec_home"
    monkeypatch.setenv("SPEC_HOME", str(spec_home))
    Preferences(prompt_stream="muted").save()
    m = _manifest_at(tmp_path, {"cloud": {"prompt_stream": "off"}})
    prefs = load_preferences()
    resolved = m.prompt_stream_enabled and not prefs.prompt_stream_muted
    assert resolved is False
