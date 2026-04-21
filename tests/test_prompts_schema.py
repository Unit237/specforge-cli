"""Tests for the `.prompts` format: schema, tools, renderer."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from spec_cli.prompts import (
    CommitMeta,
    PromptSchemaError,
    PromptsFile,
    Session,
    ToolCall,
    Turn,
    parse_prompts_text,
    validate_prompts_file,
    validate_session,
)
from spec_cli.prompts.render import (
    _basic_quote,
    _inline_table,
    _iso_z,
    prompts_filename,
    render_prompts_file,
)
from spec_cli.prompts.tools import (
    ALLOWED_TOOL_NAMES,
    scrub_secrets,
    summarize_tool_call,
)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def test_basic_quote_escapes_special_chars():
    assert _basic_quote("hello") == '"hello"'
    assert _basic_quote('a "b" c') == '"a \\"b\\" c"'
    assert _basic_quote("line\nbreak") == '"line\\nbreak"'
    assert _basic_quote("back\\slash") == '"back\\\\slash"'


def test_iso_z_forces_utc():
    naive = datetime(2026, 3, 10, 11, 47, 12)
    assert _iso_z(naive) == "2026-03-10T11:47:12Z"
    with_tz = datetime(2026, 3, 10, 11, 47, 12, tzinfo=timezone.utc)
    assert _iso_z(with_tz) == "2026-03-10T11:47:12Z"


def test_inline_table_sorts_keys():
    # Alphabetical ordering is part of the determinism contract.
    d = {"z": 1, "a": "two", "m": True}
    out = _inline_table(d)
    assert out == '{ a = "two", m = true, z = 1 }'


def test_inline_table_empty():
    assert _inline_table({}) == "{ }"


# ---------------------------------------------------------------------------
# Secret scrub
# ---------------------------------------------------------------------------


def test_scrub_secrets_redacts_common_patterns():
    assert "[REDACTED]" in scrub_secrets("Authorization: Bearer abc123def456ghi")
    assert "[REDACTED]" in scrub_secrets("use key sk-ant-01234567890abcdefg")
    assert "[REDACTED]" in scrub_secrets("token=supersecret_value_12345")
    assert scrub_secrets("nothing to see here") == "nothing to see here"


# ---------------------------------------------------------------------------
# Tool summarizer
# ---------------------------------------------------------------------------


def test_summarize_drops_unknown_tool():
    assert summarize_tool_call("SomeInvented", {"x": 1}) is None


def test_summarize_grep_picks_allowed_fields():
    out = summarize_tool_call(
        "Grep",
        {"pattern": "calculate_tax", "path": "billing/", "secret_field": "leaks"},
    )
    assert out == {"pattern": "calculate_tax", "path": "billing/"}


def test_summarize_write_uses_byte_count_not_content():
    content = "a" * 1000
    out = summarize_tool_call("Write", {"path": "src/app.py", "contents": content})
    assert out == {"path": "src/app.py", "bytes": 1000}


def test_summarize_edit_trims_to_first_line():
    out = summarize_tool_call(
        "Edit",
        {
            "path": "src/app.py",
            "old_string": "old line one\nold line two",
            "new_string": "new line",
        },
    )
    assert out == {
        "path": "src/app.py",
        "old_head": "old line one",
        "new_head": "new line",
    }


def test_summarize_shell_scrubs_tokens():
    out = summarize_tool_call(
        "Shell", {"command": "curl -H 'Authorization: Bearer sk-ant-abcdef1234567890'"}
    )
    assert out is not None
    assert "Bearer" not in out["command"]
    assert "[REDACTED]" in out["command"]


# ---------------------------------------------------------------------------
# Session / commit / file validation
# ---------------------------------------------------------------------------


def _minimal_session(id: str = "abc") -> Session:
    return Session(
        id=id,
        source="claude_code",
        turns=[Turn(role="user", text="hi")],
    )


def _minimal_commit() -> CommitMeta:
    return CommitMeta(
        branch="main",
        author_name="Test User",
        author_email="test@example.com",
    )


def _minimal_file(sessions: list[Session] | None = None) -> PromptsFile:
    return PromptsFile(
        commit=_minimal_commit(),
        sessions=sessions if sessions is not None else [_minimal_session()],
    )


def test_validate_accepts_minimal_session():
    validate_session(_minimal_session())


def test_validate_rejects_empty_id():
    s = _minimal_session()
    s.id = ""
    with pytest.raises(PromptSchemaError):
        validate_session(s)


def test_validate_rejects_empty_turns():
    s = _minimal_session()
    s.turns = []
    with pytest.raises(PromptSchemaError):
        validate_session(s)


def test_validate_rejects_reserved_role():
    s = _minimal_session()
    s.turns.append(Turn(role="tool_result", summary="x"))
    with pytest.raises(PromptSchemaError):
        validate_session(s)


def test_validate_rejects_user_without_text():
    s = Session(id="x", source="manual", turns=[Turn(role="user")])
    with pytest.raises(PromptSchemaError):
        validate_session(s)


def test_validate_rejects_assistant_text_when_not_verbose():
    s = Session(
        id="x",
        source="manual",
        turns=[
            Turn(role="user", text="hi"),
            Turn(role="assistant", text="long text", summary="hi"),
        ],
    )
    with pytest.raises(PromptSchemaError):
        validate_session(s)


def test_validate_rejects_unknown_visibility():
    s = _minimal_session()
    s.visibility = "semipublic"
    with pytest.raises(PromptSchemaError):
        validate_session(s)


def test_validate_rejects_unknown_outcome():
    s = _minimal_session()
    s.outcome = "pending"
    with pytest.raises(PromptSchemaError):
        validate_session(s)


def test_validate_file_rejects_missing_sessions():
    pf = _minimal_file(sessions=[])
    with pytest.raises(PromptSchemaError):
        validate_prompts_file(pf)


def test_validate_file_rejects_duplicate_ids():
    pf = _minimal_file(
        sessions=[_minimal_session("dup"), _minimal_session("dup")]
    )
    with pytest.raises(PromptSchemaError) as exc:
        validate_prompts_file(pf)
    assert "duplicate" in str(exc.value)


def test_validate_commit_rejects_empty_author_email():
    pf = _minimal_file()
    pf.commit.author_email = ""
    with pytest.raises(PromptSchemaError):
        validate_prompts_file(pf)


# ---------------------------------------------------------------------------
# Render → parse round-trip
# ---------------------------------------------------------------------------


def test_render_and_parse_roundtrip():
    ts_user = datetime(2026, 3, 10, 11, 47, 12, tzinfo=timezone.utc)
    ts_ass = datetime(2026, 3, 10, 11, 47, 35, tzinfo=timezone.utc)
    session = Session(
        id="d1714569-2799-464b-9a0e-360aced5767c",
        source="claude_code",
        started_at=ts_user,
        ended_at=ts_ass,
        cwd="/Users/alice/code/billing",
        model="claude-sonnet-4-5",
        title="Extract tax logic",
        summary="Pulled tax logic into its own module.",
        lesson="Grep call sites first.",
        tags=["refactor", "billing"],
        outcome="shipped",
        paths_touched=["billing/billing.py", "billing/tax.py"],
        turns=[
            Turn(
                role="user",
                at=ts_user,
                text="Refactor billing.py to extract the tax logic.\nKeep the interface identical.",
            ),
            Turn(
                role="assistant",
                at=ts_ass,
                summary="Mapping tax call sites before extraction.",
                tool_calls=[
                    ToolCall(
                        name="Grep",
                        args={"pattern": "calculate_tax", "path": "billing/"},
                    ),
                ],
            ),
        ],
    )

    pf = PromptsFile(
        commit=CommitMeta(
            branch="main",
            author_name="Alice Chen",
            author_email="alice@example.com",
            message="Extract tax logic",
            committed_at=ts_ass,
        ),
        sessions=[session],
    )

    rendered = render_prompts_file(pf)
    parsed = parse_prompts_text(rendered)

    assert parsed.commit.branch == "main"
    assert parsed.commit.author_name == "Alice Chen"
    assert parsed.commit.author_email == "alice@example.com"
    assert len(parsed.sessions) == 1
    ps = parsed.sessions[0]
    assert ps.id == session.id
    assert ps.source == "claude_code"
    assert ps.model == "claude-sonnet-4-5"
    assert ps.title == "Extract tax logic"
    assert ps.outcome == "shipped"
    assert ps.visibility == "public"  # default round-trips
    assert ps.tags == ["refactor", "billing"]
    assert ps.paths_touched == ["billing/billing.py", "billing/tax.py"]
    assert len(ps.turns) == 2

    # User text round-trips exactly (aside from trailing newline our writer
    # deliberately adds to multi-line content).
    assert "Refactor billing.py" in ps.turns[0].text
    assert ps.turns[1].summary == "Mapping tax call sites before extraction."
    assert ps.turns[1].tool_calls[0].name == "Grep"
    assert ps.turns[1].tool_calls[0].args == {
        "pattern": "calculate_tax",
        "path": "billing/",
    }


def test_render_is_deterministic():
    ts = datetime(2026, 3, 10, 11, 47, 12, tzinfo=timezone.utc)
    a = _minimal_file(
        sessions=[
            Session(
                id="abc-123",
                source="claude_code",
                started_at=ts,
                turns=[Turn(role="user", at=ts, text="hi")],
            )
        ]
    )
    b = _minimal_file(
        sessions=[
            Session(
                id="abc-123",
                source="claude_code",
                started_at=ts,
                turns=[Turn(role="user", at=ts, text="hi")],
            )
        ]
    )
    assert render_prompts_file(a) == render_prompts_file(b)


def test_render_orders_sessions_by_started_at():
    early = datetime(2026, 3, 10, 11, 0, 0, tzinfo=timezone.utc)
    late = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
    late_session = Session(
        id="late",
        source="claude_code",
        started_at=late,
        turns=[Turn(role="user", at=late, text="second")],
    )
    early_session = Session(
        id="early",
        source="claude_code",
        started_at=early,
        turns=[Turn(role="user", at=early, text="first")],
    )
    pf = _minimal_file(sessions=[late_session, early_session])
    rendered = render_prompts_file(pf)
    # Earlier id appears before later id in output.
    assert rendered.index('id = "early"') < rendered.index('id = "late"')


def test_render_rejects_hand_built_bad_file():
    bad = _minimal_file(
        sessions=[Session(id="", source="claude_code", turns=[Turn(role="user", text="hi")])]
    )
    with pytest.raises(PromptSchemaError):
        render_prompts_file(bad)


# ---------------------------------------------------------------------------
# Schema gate
# ---------------------------------------------------------------------------


def test_parse_rejects_unknown_schema_version():
    bad = """
schema = "spec.prompts/v9.9"
[commit]
branch = "main"
author_name = "x"
author_email = "x@example.com"

[[sessions]]
id = "x"
source = "manual"

[[sessions.turns]]
role = "user"
text = "hi"
"""
    with pytest.raises(PromptSchemaError):
        parse_prompts_text(bad)


def test_parse_rejects_unknown_top_level_keys_in_session():
    bad = """
[commit]
branch = "main"
author_name = "x"
author_email = "x@example.com"

[[sessions]]
id = "x"
source = "manual"
nonsense = "boom"

[[sessions.turns]]
role = "user"
text = "hi"
"""
    with pytest.raises(PromptSchemaError):
        parse_prompts_text(bad)


def test_parse_requires_commit_block():
    bad = """
[[sessions]]
id = "x"
source = "manual"

[[sessions.turns]]
role = "user"
text = "hi"
"""
    with pytest.raises(PromptSchemaError):
        parse_prompts_text(bad)


# ---------------------------------------------------------------------------
# Filename
# ---------------------------------------------------------------------------


def test_prompts_filename_contract():
    ts = datetime(2026, 3, 10, 11, 47, 12, tzinfo=timezone.utc)
    assert prompts_filename(ts) == "2026-03-10T11-47-12Z.prompts"


# ---------------------------------------------------------------------------
# Allowlist sanity
# ---------------------------------------------------------------------------


def test_allowlist_matches_summarizers():
    # Every name in the allowlist must have a working summarizer (no
    # None-return on empty input).
    for name in ALLOWED_TOOL_NAMES:
        assert isinstance(summarize_tool_call(name, {}), dict)
