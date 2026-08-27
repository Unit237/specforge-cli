from __future__ import annotations

import json
from pathlib import Path

import pytest

from spec_cli.constants import is_bundle_md


CONTRACT = json.loads(
    (
        Path(__file__).parent
        / "fixtures/contracts/spec-bundle-resolver-v1.json"
    ).read_text(encoding="utf-8")
)


@pytest.mark.parametrize("row", CONTRACT["cases"], ids=lambda row: row["name"])
def test_bundle_resolver_contract(row: dict) -> None:
    manifest = {"spec": row["manifest_spec"]} if row["manifest_spec"] else None
    assert is_bundle_md(
        row["path"],
        manifest=manifest,
        frontmatter=row["frontmatter"],
    ) is row["expected"]
