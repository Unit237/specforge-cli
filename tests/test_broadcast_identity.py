"""Machine-local broadcast client id (echo suppression)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_BI = _SRC / "spec_cli" / "realtime" / "broadcast_identity.py"
_spec = importlib.util.spec_from_file_location(
    "_spec_broadcast_identity_under_test", _BI
)
assert _spec is not None and _spec.loader is not None
_bi_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bi_mod)
load_or_create_broadcast_client_id = _bi_mod.load_or_create_broadcast_client_id


def test_stable_for_one_machine(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    bundle = tmp_path / "proj"
    bundle.mkdir()
    first = load_or_create_broadcast_client_id(bundle)
    second = load_or_create_broadcast_client_id(bundle)
    assert first == second
    assert len(first) >= 8


def test_different_bundle_directories_share_machine_id(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    id_a = load_or_create_broadcast_client_id(a)
    id_b = load_or_create_broadcast_client_id(b)
    assert id_a == id_b


def test_same_logical_repo_path_under_different_homes_differs(
    tmp_path: Path, monkeypatch
) -> None:
    """Two machines with identical username + clone path get different client ids."""
    rel = Path("icloud") / "work" / "spec"
    home1 = tmp_path / "home1"
    home2 = tmp_path / "home2"
    bundle1 = home1 / rel
    bundle2 = home2 / rel
    bundle1.mkdir(parents=True)
    bundle2.mkdir(parents=True)

    monkeypatch.setenv("HOME", str(home1))
    id1 = load_or_create_broadcast_client_id(bundle1)
    monkeypatch.setenv("HOME", str(home2))
    id2 = load_or_create_broadcast_client_id(bundle2)
    assert id1 != id2
