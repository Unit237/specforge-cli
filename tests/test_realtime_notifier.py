"""Tests for the live-stream Notifier render helpers.

The Notifier itself prints to a Rich console (hard to assert against
in unit tests), so we focus on the pure formatting helpers that drive
the new header chips — cwd shortening, session id rendering, paths
collapsing — and the opt-in alert path. The alert tests stub out
``subprocess`` / ``sys.stderr`` so nothing leaks into the test runner.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from collections import deque
from io import StringIO
from unittest.mock import MagicMock

import pytest
from rich.console import Console
from rich.theme import Theme

from spec_cli.realtime.critic import SEV_HIGH, SEV_WARN, Critique
from spec_cli.realtime.events import IncomingEvent, ToolCallPayload
from spec_cli.realtime.notifier import (
    Notifier,
    _format_tool_call_line,
    _paths_chip,
    _session_color,
    _short_cwd,
    _short_session,
    _strip_code_blocks,
    format_live_event_clock,
)


def test_format_live_event_clock_includes_date():
    dt = datetime(2026, 5, 15, 13, 7, 9, tzinfo=timezone.utc)
    out = format_live_event_clock(dt)
    assert out == dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def test_assistant_visible_prose_skips_redacted_text_for_summary():
    """Cursor tool steps often ship text=[REDACTED] with a real summary."""
    body = Notifier._assistant_visible_prose(
        "[REDACTED]",
        "Investigating the live feed.",
    )
    assert body == "Investigating the live feed."
    assert Notifier._assistant_preview_is_meaningful(
        "[REDACTED]", "Investigating the live feed."
    )
    assert not Notifier._assistant_preview_is_meaningful("[REDACTED]", "[REDACTED]")


# ── _short_cwd ────────────────────────────────────────────────────


def test_short_cwd_strips_home_to_tilde(monkeypatch):
    monkeypatch.setenv("HOME", "/Users/alice")
    assert _short_cwd("/Users/alice/code/widgets") == "~/code/widgets"


def test_short_cwd_returns_path_unchanged_when_not_in_home(monkeypatch):
    monkeypatch.setenv("HOME", "/Users/alice")
    assert _short_cwd("/srv/repos/billing") == "/srv/repos/billing"


def test_short_cwd_collapses_very_long_paths_to_last_two_segments(
    monkeypatch,
):
    monkeypatch.setenv("HOME", "/Users/alice")
    long_path = "/Users/alice/very/deeply/nested/long/path/to/finally/repo"
    out = _short_cwd(long_path)
    # Either the tilde form is short enough, or it collapsed; in both
    # cases the result must end with the actual repo name so the
    # reviewer recognises the bundle.
    assert out is not None
    assert out.endswith("/repo")
    assert len(out) <= 41  # leaves room for one trailing character


def test_short_cwd_handles_none_and_empty():
    assert _short_cwd(None) is None
    assert _short_cwd("") is None
    assert _short_cwd("   ") is None


# ── _short_session ────────────────────────────────────────────────


def test_short_session_truncates_to_six_chars():
    assert _short_session("abc12345678") == "abc123"


def test_short_session_handles_short_or_missing_input():
    assert _short_session("abc") == "abc"
    assert _short_session(None) is None
    assert _short_session("") is None


# ── _session_color ────────────────────────────────────────────────


def test_session_color_is_stable_for_same_id():
    """Two events from the same Cursor / Codex session must paint
    the chip the same color so a reviewer can follow one thread
    visually as two teammates' prompts interleave."""
    a = _session_color("aaaa-1111")
    b = _session_color("aaaa-1111")
    assert a == b


def test_session_color_falls_back_when_id_is_blank():
    """No session id → neutral muted color so we don't leak a
    misleading 'this row belongs to a thread' signal."""
    assert _session_color(None) == "#9aa3b2"
    assert _session_color("") == "#9aa3b2"
    assert _session_color("   ") == "#9aa3b2"


# ── _strip_code_blocks ────────────────────────────────────────────


def test_strip_code_blocks_collapses_fenced_block_with_lang():
    """``spec team watch`` default view replaces fenced code with a
    compact placeholder so the prose stays scannable."""
    body = (
        "Here is what I changed:\n\n"
        "```python\n"
        "def foo():\n"
        "    return 1 + 2\n"
        "```\n\n"
        "That should do it."
    )
    out = _strip_code_blocks(body)
    assert "def foo" not in out
    assert "Here is what I changed" in out
    assert "That should do it" in out
    assert "[code: python" in out


def test_strip_code_blocks_keeps_prose_intact_with_no_code():
    """Pure-prose assistant replies must be unchanged — we don't want
    the stripper to mangle bullet lists or backtick-wrapped
    identifiers."""
    body = "Use the `auth_helper` function — it's idempotent."
    assert _strip_code_blocks(body) == body


# ── _format_tool_call_line ────────────────────────────────────────


def test_format_tool_call_line_emits_canonical_one_liners():
    """One-line summaries for each tool kind — same shape the
    auto-critic scans, so a destructive Bash or pasted secret in
    Grep both surface to the reviewer's eye."""
    assert (
        _format_tool_call_line(ToolCallPayload(name="Bash", args={"command": "rm -rf foo"}))
        == 'Bash "rm -rf foo"'
    )
    assert _format_tool_call_line(
        ToolCallPayload(name="Edit", args={"path": "src/auth.py"})
    ).endswith("auth.py")
    assert _format_tool_call_line(
        ToolCallPayload(name="Read", args={"path": "main.py"})
    ).endswith("main.py")
    assert _format_tool_call_line(
        ToolCallPayload(name="Grep", args={"pattern": "TODO"})
    ) == 'Grep "TODO"'


# ── _paths_chip ────────────────────────────────────────────────────


def test_paths_chip_renders_basenames_with_overflow():
    out = _paths_chip(["a/b/c.py", "d/e/f.py", "g/h/i.py", "j/k/l.py"])
    assert out is not None
    assert "c.py" in out
    assert "f.py" in out
    # First two basenames only; remaining two summarised as +2 more.
    assert "+2 more" in out


def test_paths_chip_drops_empty_input():
    assert _paths_chip(None) is None
    assert _paths_chip([]) is None
    assert _paths_chip(["", None, ""]) is None  # type: ignore[list-item]


# ── pending user prompt (assistant context) ───────────────────────


def _recording_console() -> Console:
    """Console compatible with :mod:`spec_cli.ui` theme tokens."""
    return Console(
        record=True,
        width=120,
        theme=Theme(
            {
                "sf.mint": "bold #3ddab4",
                "sf.reject": "bold #ff5a6a",
                "sf.warn": "bold #f0b86e",
                "sf.muted": "dim #9aa3b2",
                "sf.point": "bold #7de3ff",
                "sf.label": "bold #c7c9d1",
            }
        ),
        highlight=False,
    )


def test_viewer_handle_skips_no_reply_tracking_for_own_user_prompt(
    monkeypatch,
):
    """``spec team watch`` passes viewer_handle so ⏳ never targets self."""
    import spec_cli.realtime.notifier as notifier_mod

    cap = _recording_console()
    monkeypatch.setattr(notifier_mod, "console", cap)
    ts = datetime.now(timezone.utc)
    sid = "sess-self"
    pid = 42
    own = IncomingEvent(
        id=101,
        project_id=pid,
        session_id=sid,
        source="cursor",
        role="user",
        branch="main",
        commit_sha=None,
        model=None,
        summary=None,
        text="My question",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=7,
        author_handle="jon",
        author_name="Jon",
        author_avatar_url=None,
    )
    n = Notifier(critic_enabled=False, viewer_handle="jon")
    n.show(own)
    assert (pid, sid) not in n._open_sessions

    peer = IncomingEvent(
        id=102,
        project_id=pid,
        session_id="sess-peer",
        source="cursor",
        role="user",
        branch="main",
        commit_sha=None,
        model=None,
        summary=None,
        text="Peer question",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=8,
        author_handle="alice",
        author_name="Alice",
        author_avatar_url=None,
    )
    n.show(peer)
    assert (pid, "sess-peer") in n._open_sessions


def test_review_feed_full_bodies_uses_schema_max_for_user_and_assistant() -> None:
    from spec_cli.prompts.schema import MAX_TURN_TEXT_CHARS
    from spec_cli.realtime import notifier as notifier_mod

    n = Notifier(critic_enabled=False, review_feed_full_bodies=True)
    assert n._user_preview_limit() == MAX_TURN_TEXT_CHARS
    assert n._assistant_body_limit_chars() == MAX_TURN_TEXT_CHARS
    assert n._error_preview_limit() == MAX_TURN_TEXT_CHARS
    compact = Notifier(
        critic_enabled=False, review_feed_full_bodies=True, compact=True
    )
    assert compact._user_preview_limit() == notifier_mod._PREVIEW_USER[1]


def test_digest_mode_assistant_live_cap_overrides_schema_max() -> None:
    from spec_cli.prompts.schema import MAX_TURN_TEXT_CHARS

    n = Notifier(
        critic_enabled=False,
        review_feed_full_bodies=True,
        assistant_live_cap=400,
    )
    assert n._user_preview_limit() == MAX_TURN_TEXT_CHARS
    assert n._assistant_body_limit_chars() == 400
    assert n._assistant_body_limit_for_completed_pair() == MAX_TURN_TEXT_CHARS


def test_assistant_visible_prose_prefers_text_over_long_summary() -> None:
    """Long ``summary`` must not be prepended as a wall — body column wins."""
    tail = "ONLY_IN_SUMMARY_" + "x" * 200
    long_summary = ("section " * 80).strip() + "\n" + tail
    assert len(long_summary) > 400
    short_text = "early streaming snapshot"
    assert tail not in short_text
    out = Notifier._assistant_visible_prose(short_text, long_summary)
    assert out == short_text


def test_assistant_visible_prose_short_headline_prepended_when_distinct() -> None:
    out = Notifier._assistant_visible_prose("body text here", "Headline")
    assert out.startswith("Headline")
    assert "body text here" in out


def test_assistant_visible_prose_returns_longer_when_text_embedded_in_summary() -> None:
    """When the full prose lives in ``summary`` and ``text`` is a substring."""
    t = "tiny"
    s = "before tiny after"
    assert Notifier._assistant_visible_prose(t, s) == s


def test_show_completed_pair_digest_mode_does_not_truncate_merged_assistant(
    monkeypatch,
) -> None:
    """Coalesced Q/A is the only assistant surface in default team watch."""
    from spec_cli.prompts.schema import MAX_TURN_TEXT_CHARS

    import spec_cli.realtime.notifier as notifier_mod

    cap = _recording_console()
    monkeypatch.setattr(notifier_mod, "console", cap)
    ts = datetime.now(timezone.utc)
    long_body = "word " * 300  # 1800+ chars — would be clipped at 400 in ``show()``.
    assert len(long_body) > 400
    user = IncomingEvent(
        id=400,
        project_id=1,
        session_id="sess-long",
        source="cursor",
        role="user",
        branch="main",
        commit_sha=None,
        model=None,
        summary=None,
        text="go",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=7,
        author_handle="jon",
        author_name="Jon",
        author_avatar_url=None,
    )
    assistant = IncomingEvent(
        id=401,
        project_id=1,
        session_id="sess-long",
        source="cursor",
        role="assistant",
        branch="main",
        commit_sha=None,
        model="default",
        summary="done",
        text=long_body,
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=7,
        author_handle="jon",
        author_name="Jon",
        author_avatar_url=None,
    )
    n = Notifier(
        critic_enabled=False,
        review_feed_full_bodies=True,
        assistant_live_cap=400,
    )
    n.show_completed_pair(user, assistant)
    out = cap.export_text()
    # Rich wraps at the recording console width — do not require one-line substring match.
    before_footer = out.split("● turn complete")[0]
    assert len(before_footer) > 1200
    assert before_footer.count("word") >= 300
    assert "…" not in before_footer
    assert n._assistant_body_limit_chars() == 400
    assert n._assistant_body_limit_for_completed_pair() == MAX_TURN_TEXT_CHARS


def test_default_team_watch_strips_code_blocks_from_assistant_body(monkeypatch):
    """``spec team watch`` default render must show the AI's
    narration with embedded code blocks collapsed to a compact
    ``[code: lang ~N lines]`` placeholder. The user toggle for raw
    code is ``--show-tool-runs``; without it, fenced code in the
    prose body would push every reply off-screen on a busy pane."""
    import spec_cli.realtime.notifier as notifier_mod

    cap = _recording_console()
    monkeypatch.setattr(notifier_mod, "console", cap)
    ts = datetime.now(timezone.utc)
    ev = IncomingEvent(
        id=200,
        project_id=1,
        session_id="sess-strip",
        source="cursor",
        role="assistant",
        branch="main",
        commit_sha=None,
        model="default",
        summary="Here is the patch.",
        text=(
            "Here is the patch you asked for:\n\n"
            "```python\n"
            "def add(a, b):\n"
            "    return a + b\n"
            "```\n\n"
            "Let me know if anything else needs tweaking."
        ),
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=7,
        author_handle="jon",
        author_name="Jon",
        author_avatar_url=None,
    )
    n = Notifier(critic_enabled=False)
    n.show(ev)
    out = cap.export_text()
    assert "Here is the patch you asked for" in out
    assert "Let me know if anything else" in out
    # Raw code body must not survive the stripper.
    assert "def add" not in out
    assert "[code: python" in out


def test_show_tool_runs_renders_grouped_tool_digest(monkeypatch):
    """``--show-tool-runs`` groups calls by kind with representative
    targets so busy agent loops remain readable without hiding the signal.
    """
    import spec_cli.realtime.notifier as notifier_mod

    cap = _recording_console()
    monkeypatch.setattr(notifier_mod, "console", cap)
    ts = datetime.now(timezone.utc)
    ev = IncomingEvent(
        id=201,
        project_id=1,
        session_id="sess-tools",
        source="cursor",
        role="assistant",
        branch="main",
        commit_sha=None,
        model="default",
        summary="Touched a few files.",
        text="Touched a few files.",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=7,
        author_handle="jon",
        author_name="Jon",
        author_avatar_url=None,
        tool_calls=[
            ToolCallPayload(name="Read", args={"path": "auth.py"}),
            ToolCallPayload(name="Read", args={"path": "users.py"}),
            ToolCallPayload(name="Edit", args={"path": "auth.py"}),
            ToolCallPayload(name="Bash", args={"command": "pytest -q"}),
        ],
    )
    n = Notifier(critic_enabled=False, show_tool_runs=True)
    n.show(ev)
    out = cap.export_text()
    # The header carries the count so reviewers know what they're
    # scrolling past.
    assert "4 tool runs" in out
    assert "Read ×2" in out
    assert "Edit ×1" in out
    assert "Bash ×1" in out
    assert "auth.py · users.py" in out
    assert '"pytest -q"' in out


def test_show_tool_runs_compact_still_renders_tool_calls(monkeypatch):
    """``--compact`` used to ``return`` before ``_render_tool_calls``,
    so ``spec team watch --compact --show-tool-runs`` silently dropped
    the tool list — the flag must still expand tools under the one-line
    summary."""
    import spec_cli.realtime.notifier as notifier_mod

    cap = _recording_console()
    monkeypatch.setattr(notifier_mod, "console", cap)
    ts = datetime.now(timezone.utc)
    ev = IncomingEvent(
        id=203,
        project_id=1,
        session_id="sess-tools-compact",
        source="cursor",
        role="assistant",
        branch="main",
        commit_sha=None,
        model="default",
        summary="Done.",
        text="Done.",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=7,
        author_handle="jon",
        author_name="Jon",
        author_avatar_url=None,
        tool_calls=[ToolCallPayload(name="Read", args={"path": "x.py"})],
    )
    n = Notifier(critic_enabled=False, compact=True, show_tool_runs=True)
    n.show(ev)
    out = cap.export_text()
    assert "1 tool run" in out
    assert "Read ×1" in out
    assert "x.py" in out


def test_codex_phases_and_chat_title_have_distinct_text_labels(monkeypatch):
    """Prompts, progress, and conclusions must be distinguishable without
    relying on color, and every interleaved row must name its Codex chat.
    """
    import spec_cli.realtime.notifier as notifier_mod

    cap = _recording_console()
    monkeypatch.setattr(notifier_mod, "console", cap)
    update = _ev(role="assistant", text="Still running the release gate.")
    update.phase = "commentary"
    update.title = "Improve ICP-driven next actions"
    answer = _ev(role="assistant", text="The release is deployed.")
    answer.id += 1
    answer.phase = "final_answer"
    answer.title = update.title

    n = Notifier(critic_enabled=False)
    n.show(update)
    n.show(answer)
    out = cap.export_text()

    assert "UPDATE" in out
    assert "progress for" in out
    assert "ANSWER" in out
    assert "answer to" in out
    assert out.count("Improve ICP-driven next actions") == 2


def test_transport_close_and_safe_tool_only_rows_are_not_rendered(monkeypatch):
    import spec_cli.realtime.notifier as notifier_mod

    cap = _recording_console()
    monkeypatch.setattr(notifier_mod, "console", cap)
    close = _ev(role="assistant_closed", text=None)
    tool_only = _ev(role="assistant", text=None)
    tool_only.summary = "ran 2 tools: Read app.py, Bash pytest"
    tool_only.tool_calls = [ToolCallPayload(name="Read", args={"path": "app.py"})]

    n = Notifier(critic_enabled=False)
    n.show(close)
    n.show(tool_only)

    assert cap.export_text() == ""


def test_show_completed_pair_renders_paired_banner(monkeypatch):
    """``show_completed_pair`` is the second-pass Q/A bundle for team watch."""
    import spec_cli.realtime.notifier as notifier_mod

    cap = _recording_console()
    monkeypatch.setattr(notifier_mod, "console", cap)
    ts = datetime.now(timezone.utc)
    user = IncomingEvent(
        id=300,
        project_id=1,
        session_id="sess-pair",
        source="cursor",
        role="user",
        branch="main",
        commit_sha=None,
        model=None,
        summary=None,
        text="Why is this broken?",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=7,
        author_handle="jon",
        author_name="Jon",
        author_avatar_url=None,
    )
    assistant = IncomingEvent(
        id=301,
        project_id=1,
        session_id="sess-pair",
        source="cursor",
        role="assistant",
        branch="main",
        commit_sha=None,
        model="default",
        summary="Fixed.",
        text="Fixed.",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=7,
        author_handle="jon",
        author_name="Jon",
        author_avatar_url=None,
    )
    n = Notifier(critic_enabled=False)
    n.show_completed_pair(user, assistant)
    out = cap.export_text()
    assert "paired reply" in out
    assert "#300" in out and "#301" in out
    assert "Why is this broken?" in out
    assert "Fixed." in out
    assert len(n._recent_completed_pairs) == 1


def test_default_team_watch_does_not_render_tool_calls(monkeypatch):
    """Without ``--show-tool-runs``, the structured tool list must not
    leak into the pane — the default view is prose only."""
    import spec_cli.realtime.notifier as notifier_mod

    cap = _recording_console()
    monkeypatch.setattr(notifier_mod, "console", cap)
    ts = datetime.now(timezone.utc)
    ev = IncomingEvent(
        id=202,
        project_id=1,
        session_id="sess-default",
        source="cursor",
        role="assistant",
        branch="main",
        commit_sha=None,
        model="default",
        summary="Looked something up.",
        text="Looked something up.",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=7,
        author_handle="jon",
        author_name="Jon",
        author_avatar_url=None,
        tool_calls=[ToolCallPayload(name="Bash", args={"command": "rm -rf cache"})],
    )
    n = Notifier(critic_enabled=False, show_tool_runs=False)
    n.show(ev)
    out = cap.export_text()
    assert "Looked something up" in out
    assert "tool run" not in out
    assert "Bash" not in out


def test_assistant_shows_pending_user_prompt_line(monkeypatch):
    import spec_cli.realtime.notifier as notifier_mod

    cap = _recording_console()
    monkeypatch.setattr(notifier_mod, "console", cap)
    ts = datetime.now(timezone.utc)
    sid = "composer-shared"
    pid = 42
    user = IncomingEvent(
        id=101,
        project_id=pid,
        session_id=sid,
        source="cursor",
        role="user",
        branch="main",
        commit_sha=None,
        model=None,
        summary=None,
        text="Where is the hero section and the install curl snippet?",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=7,
        author_handle="jon",
        author_name="Jon",
        author_avatar_url=None,
    )
    assistant = IncomingEvent(
        id=102,
        project_id=pid,
        session_id=sid,
        source="cursor",
        role="assistant",
        branch="main",
        commit_sha=None,
        model="default",
        summary="Searching the codebase for the landing page hero.",
        text="Searching the codebase for the landing page hero.",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=7,
        author_handle="jon",
        author_name="Jon",
        author_avatar_url=None,
    )
    n = Notifier(critic_enabled=False)
    n.show(user)
    n.show(assistant)
    out = cap.export_text()
    assert "Where is the hero section" in out
    assert "⤷ prompt" in out


def test_second_assistant_does_not_repeat_stale_prompt(monkeypatch):
    import spec_cli.realtime.notifier as notifier_mod

    cap = _recording_console()
    monkeypatch.setattr(notifier_mod, "console", cap)
    ts = datetime.now(timezone.utc)
    sid = "composer-shared-2"
    pid = 43
    user = IncomingEvent(
        id=201,
        project_id=pid,
        session_id=sid,
        source="cursor",
        role="user",
        branch="main",
        commit_sha=None,
        model=None,
        summary=None,
        text="First question only",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=7,
        author_handle="jon",
        author_name="Jon",
        author_avatar_url=None,
    )
    a1 = IncomingEvent(
        id=202,
        project_id=pid,
        session_id=sid,
        source="cursor",
        role="assistant",
        branch="main",
        commit_sha=None,
        model="m",
        summary="reply one",
        text="reply one",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=7,
        author_handle="jon",
        author_name="Jon",
        author_avatar_url=None,
    )
    a2 = IncomingEvent(
        id=203,
        project_id=pid,
        session_id=sid,
        source="cursor",
        role="assistant",
        branch="main",
        commit_sha=None,
        model="m",
        summary="reply two",
        text="reply two",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=7,
        author_handle="jon",
        author_name="Jon",
        author_avatar_url=None,
    )
    n = Notifier(critic_enabled=False)
    n.show(user)
    n.show(a1)
    n.show(a2)
    assert cap.export_text().count("⤷ prompt") == 1


def test_assistant_pairs_prompt_from_buffer_when_pending_missing(
    monkeypatch,
):
    """If the USER row never hit ``_pending_user_prompt``, scan buffer."""
    import spec_cli.realtime.notifier as notifier_mod

    cap = _recording_console()
    monkeypatch.setattr(notifier_mod, "console", cap)
    ts = datetime.now(timezone.utc)
    pid = 77
    sid = "cursor-sess-buf"
    user = IncomingEvent(
        id=10,
        project_id=pid,
        session_id=sid,
        source="cursor",
        role="user",
        branch="main",
        commit_sha=None,
        model=None,
        summary=None,
        text="First user instruction that must echo under the assistant.",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=7,
        author_handle="jon",
        author_name="Jon",
        author_avatar_url=None,
    )
    assistant = IncomingEvent(
        id=11,
        project_id=pid,
        session_id=sid,
        source="cursor",
        role="assistant",
        branch="main",
        commit_sha=None,
        model="default",
        summary="Short headline.",
        text="The assistant reply body.",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=7,
        author_handle="jon",
        author_name="Jon",
        author_avatar_url=None,
    )
    buf = deque([user, assistant], maxlen=50)
    n = Notifier(critic_enabled=False, pairing_buffer=buf)
    n.show(assistant)
    out = cap.export_text()
    assert "⤷ prompt" in out
    assert "First user instruction" in out


def test_assistant_prefers_full_text_over_one_line_summary(monkeypatch):
    import spec_cli.realtime.notifier as notifier_mod

    cap = _recording_console()
    monkeypatch.setattr(notifier_mod, "console", cap)
    ts = datetime.now(timezone.utc)
    sid = "sess-detail"
    pid = 99
    long_tail = "X" * 800
    assistant = IncomingEvent(
        id=501,
        project_id=pid,
        session_id=sid,
        source="cursor",
        role="assistant",
        branch="main",
        commit_sha=None,
        model="default",
        summary="Short headline only.",
        text=f"Short headline only.\n\nExpanded reasoning and code.\n{long_tail}",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=3,
        author_handle="pat",
        author_name="Pat",
        author_avatar_url=None,
    )
    n = Notifier(critic_enabled=False)
    n.show(assistant)
    out = cap.export_text()
    assert "Expanded reasoning and code." in out
    # ``export_text`` inserts hard wraps — count chars instead of a
    # contiguous 800-``X`` substring.
    assert out.count("X") >= 800


# ── _alert (notify) ───────────────────────────────────────────────


def _ev(role: str = "user", text: str | None = "hello") -> IncomingEvent:
    ts = datetime.now(timezone.utc)
    return IncomingEvent(
        id=99,
        project_id=1,
        session_id="abcdef123456",
        source="claude_code",
        role=role,
        branch="main",
        commit_sha=None,
        model=None,
        summary=None,
        text=text,
        title=None,
        cwd=None,
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=1,
        author_handle="alice",
        author_name="Alice",
        author_avatar_url=None,
    )


def test_notify_off_by_default_does_not_alert(monkeypatch):
    n = Notifier()
    called = {"bell": False, "osa": False}

    monkeypatch.setattr(
        sys, "stderr", _stub_stderr_with_bell_callback(called)
    )
    monkeypatch.setattr(
        "shutil.which", lambda _: "/usr/bin/osascript"
    )

    # Critic hit but notify is off → no bell, no osascript.
    hits = [
        Critique(
            rule="destructive-verb",
            severity=SEV_HIGH,
            msg="rm -rf detected",
            suggested_flag_kind="block",
        )
    ]
    n._render_critiques(_ev(), hits)
    assert called["bell"] is False


def test_notify_on_rings_bell_on_block_severity(monkeypatch):
    n = Notifier(notify=True)
    called = {"bell": False}
    monkeypatch.setattr(
        sys, "stderr", _stub_stderr_with_bell_callback(called)
    )
    # No osascript available — we still want the bell to ring so
    # cross-platform users get the audible cue.
    monkeypatch.setattr("shutil.which", lambda _: None)

    hits = [
        Critique(
            rule="destructive-verb",
            severity=SEV_HIGH,
            msg="rm -rf detected",
            suggested_flag_kind="block",
        )
    ]
    n._render_critiques(_ev(), hits)
    assert called["bell"] is True


def test_pairing_from_buffer_suppresses_repeat_echo_in_chain():
    """When an agent emits two assistant turns in a row for the same
    user prompt ("Time to make the fixes." then "## Fix 1: …" 3 s
    later), only the FIRST reply should carry the ``⤷ prompt`` echo.
    The second one is a continuation of the same answer — re-printing
    the one-liner under every body reads as duplicated noise."""
    buf: deque[IncomingEvent] = deque(maxlen=500)
    n = Notifier(pairing_buffer=buf)

    user_ev = _ev(role="user", text="is this fine cd /Users/mvii/...")
    user_ev.id = 100
    buf.append(user_ev)

    ai1 = _ev(role="assistant", text="Time to make the fixes.")
    ai1.id = 101
    ai1.session_id = user_ev.session_id

    # First assistant reply — the in-memory ``_pending_user_prompt``
    # popped by ``show()`` is the normal path; the buffer is the
    # fallback. We exercise the fallback path directly because that's
    # where the duplicate-echo bug lived (after the pop, every later
    # call would re-discover the same user row).
    first_echo = n._pairing_prompt_from_buffer(ai1)
    assert first_echo is not None
    assert first_echo[0] == user_ev.author_display
    buf.append(ai1)  # mirror the real watcher path

    ai2 = _ev(role="assistant", text="## Fix 1: ...")
    ai2.id = 102
    ai2.session_id = user_ev.session_id
    second_echo = n._pairing_prompt_from_buffer(ai2)
    assert second_echo is None, (
        "second assistant turn in the same session must NOT re-echo "
        "the prompt — it's a continuation of the same answer"
    )


def test_pairing_from_buffer_re_echoes_after_new_user_prompt():
    """After a fresh user prompt the chain resets — the next
    assistant turn should echo the *new* prompt, not the stale one."""
    buf: deque[IncomingEvent] = deque(maxlen=500)
    n = Notifier(pairing_buffer=buf)

    u1 = _ev(role="user", text="first question")
    u1.id = 200
    buf.append(u1)

    a1 = _ev(role="assistant", text="first answer")
    a1.id = 201
    a1.session_id = u1.session_id
    buf.append(a1)

    u2 = _ev(role="user", text="follow-up question")
    u2.id = 202
    u2.session_id = u1.session_id
    buf.append(u2)

    a2 = _ev(role="assistant", text="follow-up answer")
    a2.id = 203
    a2.session_id = u1.session_id
    echo = n._pairing_prompt_from_buffer(a2)
    assert echo is not None
    assert echo[1].startswith("follow-up"), (
        "after a new user prompt arrives, the next assistant turn "
        "must echo the *new* prompt — not the previous one"
    )


def test_record_pairing_clears_open_session_on_assistant_reply():
    """Tool-only assistant turns are filtered out before ``show()`` in
    the watcher's ``_deliver`` chain — but the user→AI pairing tracker
    must still clear or the no-reply hint fires 90 s later as if the
    AI ghosted. ``record_pairing`` is the hop the watcher calls before
    the filter to keep the tracker honest."""
    n = Notifier()
    user_ev = _ev(role="user", text="please refactor")
    # Pretend a user turn just landed.
    n.record_pairing(user_ev)
    key = (user_ev.project_id, user_ev.session_id)
    assert key in n._open_sessions, "user prompt must enter the pairing tracker"

    # And now the matching assistant reply lands. Even though the
    # caller may filter it out before calling show(), the pairing
    # tracker should already be clear.
    ai_ev = _ev(role="assistant", text=None)
    ai_ev.session_id = user_ev.session_id
    ai_ev.project_id = user_ev.project_id
    n.record_pairing(ai_ev)
    assert key not in n._open_sessions, (
        "assistant reply should clear the pairing tracker so the "
        "90 s no-reply hint does not fire spuriously"
    )


def test_record_pairing_treats_error_as_a_reply():
    """An ``error`` role frame closes the awaiting-reply tracker just
    like an assistant turn — agent timeout / refusal is a definitive
    answer, just not a happy one. Without this the no-reply hint
    would fire on top of a failed turn."""
    n = Notifier()
    user_ev = _ev(role="user", text="please refactor")
    n.record_pairing(user_ev)
    err_ev = _ev(role="error", text="tool failed")
    err_ev.session_id = user_ev.session_id
    err_ev.project_id = user_ev.project_id
    n.record_pairing(err_ev)
    assert (
        user_ev.project_id,
        user_ev.session_id,
    ) not in n._open_sessions


def test_notify_does_not_fire_for_warn_severity(monkeypatch):
    n = Notifier(notify=True)
    called = {"bell": False}
    monkeypatch.setattr(
        sys, "stderr", _stub_stderr_with_bell_callback(called)
    )
    hits = [
        Critique(
            rule="vague-intent",
            severity=SEV_WARN,
            msg="vague",
            suggested_flag_kind="warning",
        )
    ]
    n._render_critiques(_ev(), hits)
    assert called["bell"] is False


# ── helpers ───────────────────────────────────────────────────────


def _stub_stderr_with_bell_callback(state: dict) -> object:
    """Replacement stderr that records when the BEL character is
    written. Used to verify the opt-in --notify path without polluting
    the actual test runner stderr."""

    class _Sink:
        def write(self, s: str) -> None:
            if "\a" in s:
                state["bell"] = True

        def flush(self) -> None:
            pass

    return _Sink()
