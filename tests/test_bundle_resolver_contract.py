from __future__ import annotations

import json
from pathlib import Path

import pytest

from spec_cli.constants import is_bundle_md


CONTRACT = json.loads(
    (Path(__file__).parent / "fixtures/contracts/spec-bundle-resolver-v1.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("row", CONTRACT["cases"], ids=lambda row: row["name"])
def test_bundle_resolver_contract(row: dict[str, object]) -> None:
    manifest_spec = row.get("manifest_spec")
    manifest = {"spec": manifest_spec} if manifest_spec is not None else None
    assert is_bundle_md(
        str(row["path"]),
        manifest=manifest,
        frontmatter=row.get("frontmatter"),
    ) is row["expected"]
