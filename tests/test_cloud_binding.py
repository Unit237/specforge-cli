from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from spec_cli.cloud_binding import ensure_cloud_binding
from spec_cli.config import load_manifest


def test_connect_stamps_cloud_identity_without_uploading(tmp_path: Path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "spec.yaml").write_text(
        'schema: "spec/v0.1"\nname: Demo Project\ncloud:\n  project: demo-project\n',
        encoding="utf-8",
    )
    calls: list[tuple[str, str]] = []

    class FakeClient:
        def __init__(self, _credentials):
            pass

        def resolve_project(self, handle, slug):
            calls.append((handle, slug))
            return {
                "id": 7,
                "slug": slug,
                "owner_handle": handle,
                "bundle_id": "bdl_123",
            }

    monkeypatch.setattr("spec_cli.cloud_binding.CloudClient", FakeClient)
    result = ensure_cloud_binding(
        root,
        credentials=SimpleNamespace(access_token="token", user_handle="jon"),
    )

    assert calls == [("jon", "demo-project")]
    assert result.project == "jon/demo-project"
    manifest = load_manifest(root)
    assert manifest.cloud_project == "jon/demo-project"
    assert manifest.cloud_bundle_id == "bdl_123"
