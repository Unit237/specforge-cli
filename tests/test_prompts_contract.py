from __future__ import annotations

import json
from pathlib import Path

import pytest

from spec_cli.prompts.schema import parse_prompts_text, validate_prompts_file


CONTRACT = json.loads(
    (Path(__file__).parent / "fixtures/contracts/spec-prompts-v2.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("row", CONTRACT["cases"], ids=lambda row: row["name"])
def test_prompts_contract(row: dict) -> None:
    parsed = parse_prompts_text(row["text"])
    validate_prompts_file(parsed)
    expected = row["expected"]
    assert len(parsed.sessions) == expected["sessions"]
    assert parsed.sessions[0].id == expected["first_session_id"]
    assert parsed.sessions[0].turns[0].role == expected["first_turn_role"]
    assert parsed.sessions[0].turns[0].text == expected["first_turn_text"]
    if "last_turn_role" in expected:
        assert parsed.sessions[0].turns[-1].role == expected["last_turn_role"]
