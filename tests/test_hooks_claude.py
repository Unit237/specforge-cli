"""Tests for ``spec hooks claude-pre-tool-use`` and the Claude
settings installer.

Covers four contracts the hook must honour:

* **Edit-class tool name + conflicting file → exit 0 + warning**
  (warn-only, the friendly default).
* **Same with --block → exit 2 + warning** (firm coordination).
* **Edit-class tool name + non-conflicting file → exit 0, silent**.
* **Non-edit tool name (Read, Bash, …) → exit 0, silent** even when
  the same file is being edited by a teammate. We don't get in the
  way of file reads or shell commands.

Plus the ``install-claude`` programmatic helper round-trip:
preserves unrelated user-authored entries and replaces only the
Spec-managed block on re-runs (idempotency).
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


SPEC_BIN = [sys.executable, "-m", "spec_cli"]


# ── helpers ────────────────────────────────────────────────────────


def _write_team_presence(bundle_root: Path, files_index: dict) -> None:
    """Write a minimal ``team-presence.json`` with just the inverted
    index — the only field the hook actually consults.
    """
    spec_dir = bundle_root / ".spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    body = {
        "schema": 1,
        "updated_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "self": None,
        "members": [],
        "files_index": files_index,
    }
    (spec_dir / "team-presence.json").write_text(
        json.dumps(body, indent=2), encoding="utf-8"
    )


def _make_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "spec.yaml").write_text("name: demo\n", encoding="utf-8")
    return bundle


def _run_hook(stdin_payload: dict, *, args: list[str] | None = None) -> subprocess.CompletedProcess:
    args = args or []
    return subprocess.run(
        SPEC_BIN + ["hooks", "claude-pre-tool-use", *args],
        input=json.dumps(stdin_payload),
        capture_output=True,
        text=True,
    )


def test_user_prompt_hook_prints_coordination_brief(tmp_path):
    bundle = _make_bundle(tmp_path)
    spec_dir = bundle / ".spec"
    spec_dir.mkdir()
    (spec_dir / "team-coordination.md").write_text(
        "# Active agent rounds\n\n- Bob is changing auth.\n", encoding="utf-8"
    )

    result = subprocess.run(
        SPEC_BIN + ["hooks", "claude-user-prompt"],
        cwd=bundle,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Bob is changing auth" in result.stdout


def _stderr_warnings_only(raw: str) -> str:
    """Filter the bookkeeping lines the hook always emits (lock id)
    so legacy tests can keep asserting on user-visible warnings."""
    keep: list[str] = []
    for line in raw.splitlines():
        if line.strip().startswith("spec-lock-id:"):
            continue
        keep.append(line)
    return "\n".join(keep).strip()


# ── conflict detection ─────────────────────────────────────────────


def test_hook_surfaces_unknown_when_no_presence_data(tmp_path):
    """No ``team-presence.json`` is unknown, not evidence of safety.

    The hook still emits a ``spec-lock-id:`` line on stderr because
    it takes a local active-edit lock regardless of the team-presence
    state. Warn-only mode preserves edit availability while making the
    degraded coordination state visible.
    """
    bundle = _make_bundle(tmp_path)
    target = bundle / "auth.py"
    target.write_text("x", encoding="utf-8")
    res = _run_hook(
        {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    )
    assert res.returncode == 0
    assert "coordination state unknown" in _stderr_warnings_only(res.stderr)


def test_hook_block_mode_refuses_unknown_coordination_state(tmp_path):
    bundle = _make_bundle(tmp_path)
    target = bundle / "auth.py"
    target.write_text("x", encoding="utf-8")

    res = _run_hook(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(target)},
        },
        args=["--block"],
    )

    assert res.returncode == 3
    assert "coordination state unknown" in _stderr_warnings_only(res.stderr)


def test_hook_warns_on_conflict_default(tmp_path):
    bundle = _make_bundle(tmp_path)
    target = bundle / "auth.py"
    target.write_text("x", encoding="utf-8")
    _write_team_presence(
        bundle,
        {
            "auth.py": [
                {
                    "handle": "alice",
                    "name": "Alice",
                    "lines_added": 12,
                    "lines_removed": 3,
                    "untracked": False,
                    "self": False,
                }
            ]
        },
    )
    res = _run_hook(
        {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    )
    assert res.returncode == 0  # warn-only
    assert "@alice" in res.stderr
    assert "auth.py" in res.stderr


def test_hook_uses_live_task_claims(tmp_path):
    bundle = _make_bundle(tmp_path)
    target = bundle / "auth.py"
    target.write_text("x", encoding="utf-8")
    _write_team_presence(bundle, {})
    (bundle / ".spec" / "team-coordination.json").write_text(
        json.dumps(
            {
                "schema": "spec.team-coordination/v1",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "active": [],
                "recent_outcomes": [],
                "files_index": {
                    "auth.py": [
                        {
                            "kind": "task_claim",
                            "agent": "codex",
                            "author": "@alice",
                            "session_id": "codex-a",
                            "objective": "Refactor auth",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    result = _run_hook(
        {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    )

    assert result.returncode == 0
    assert "Refactor auth" in result.stderr
    assert "codex-a" in result.stderr


def test_hook_blocks_on_conflict_with_flag(tmp_path):
    bundle = _make_bundle(tmp_path)
    target = bundle / "auth.py"
    target.write_text("x", encoding="utf-8")
    _write_team_presence(
        bundle,
        {
            "auth.py": [
                {
                    "handle": "alice",
                    "name": "Alice",
                    "lines_added": 12,
                    "lines_removed": 3,
                    "untracked": False,
                    "self": False,
                }
            ]
        },
    )
    res = _run_hook(
        {"tool_name": "Edit", "tool_input": {"file_path": str(target)}},
        args=["--block"],
    )
    assert res.returncode == 2
    assert "@alice" in res.stderr


def test_hook_silent_when_no_overlap(tmp_path):
    bundle = _make_bundle(tmp_path)
    target = bundle / "auth.py"
    target.write_text("x", encoding="utf-8")
    _write_team_presence(
        bundle,
        {
            "other_file.py": [
                {
                    "handle": "alice",
                    "name": "Alice",
                    "lines_added": 1,
                    "lines_removed": 0,
                    "untracked": False,
                    "self": False,
                }
            ]
        },
    )
    res = _run_hook(
        {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    )
    assert res.returncode == 0
    assert _stderr_warnings_only(res.stderr) == ""


def test_hook_ignores_self_overlap(tmp_path):
    """The hook is for *teammates* — never warn the user about their
    own dirty files. Self entries are filtered out by the
    ``self == True`` flag in the inverted index."""
    bundle = _make_bundle(tmp_path)
    target = bundle / "auth.py"
    target.write_text("x", encoding="utf-8")
    _write_team_presence(
        bundle,
        {
            "auth.py": [
                {
                    "handle": "me",
                    "name": "Me",
                    "lines_added": 1,
                    "lines_removed": 0,
                    "untracked": False,
                    "self": True,
                }
            ]
        },
    )
    res = _run_hook(
        {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    )
    assert res.returncode == 0
    assert _stderr_warnings_only(res.stderr) == ""


@pytest.mark.parametrize("tool_name", ["Read", "Bash", "Grep", "Glob"])
def test_hook_silent_on_non_edit_tools(tmp_path, tool_name):
    """Read / Bash / Grep / Glob never trigger the hook even when the
    file *is* being edited by a teammate. Reads are non-destructive
    and shell commands have their own safety story."""
    bundle = _make_bundle(tmp_path)
    target = bundle / "auth.py"
    target.write_text("x", encoding="utf-8")
    _write_team_presence(
        bundle,
        {
            "auth.py": [
                {
                    "handle": "alice",
                    "name": "Alice",
                    "lines_added": 12,
                    "lines_removed": 3,
                    "untracked": False,
                    "self": False,
                }
            ]
        },
    )
    res = _run_hook(
        {"tool_name": tool_name, "tool_input": {"file_path": str(target)}}
    )
    assert res.returncode == 0
    assert res.stderr.strip() == ""


@pytest.mark.parametrize("tool_name", ["Edit", "MultiEdit", "Write", "NotebookEdit", "StrReplace", "Delete"])
def test_hook_fires_on_every_edit_class_tool(tmp_path, tool_name):
    """Edit / MultiEdit / Write / NotebookEdit / StrReplace / Delete
    must all be in scope. Adding a new edit-class tool to Claude's
    surface and forgetting to wire it here would leave a hole."""
    bundle = _make_bundle(tmp_path)
    target = bundle / "auth.py"
    target.write_text("x", encoding="utf-8")
    _write_team_presence(
        bundle,
        {
            "auth.py": [
                {
                    "handle": "alice",
                    "name": "Alice",
                    "lines_added": 1,
                    "lines_removed": 0,
                    "untracked": False,
                    "self": False,
                }
            ]
        },
    )
    payload = {"tool_name": tool_name, "tool_input": {"file_path": str(target)}}
    if tool_name == "NotebookEdit":
        payload["tool_input"] = {"notebook_path": str(target)}
    res = _run_hook(payload)
    assert "@alice" in res.stderr


def test_hook_handles_malformed_stdin(tmp_path):
    """A garbled hook input must fail open. Better to let an edit
    through than to block on a Claude Code shape change we don't
    recognise."""
    res = subprocess.run(
        SPEC_BIN + ["hooks", "claude-pre-tool-use"],
        input="not json at all",
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0


def test_hook_handles_empty_stdin(tmp_path):
    res = subprocess.run(
        SPEC_BIN + ["hooks", "claude-pre-tool-use"],
        input="",
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0


def test_hook_warns_on_multi_file_edit(tmp_path):
    """``MultiEdit`` can ship a list of edits each targeting the same
    or different files; the hook should report every conflicting
    path, not just the first."""
    bundle = _make_bundle(tmp_path)
    a = bundle / "a.py"
    b = bundle / "b.py"
    a.write_text("x", encoding="utf-8")
    b.write_text("x", encoding="utf-8")
    _write_team_presence(
        bundle,
        {
            "a.py": [
                {"handle": "alice", "name": "Alice", "lines_added": 1, "lines_removed": 0, "untracked": False, "self": False}
            ],
            "b.py": [
                {"handle": "bob", "name": "Bob", "lines_added": 1, "lines_removed": 0, "untracked": False, "self": False}
            ],
        },
    )
    payload = {
        "tool_name": "MultiEdit",
        "tool_input": {
            "file_path": str(a),
            "edits": [
                {"file_path": str(a)},
                {"file_path": str(b)},
            ],
        },
    }
    res = _run_hook(payload)
    assert "a.py" in res.stderr
    assert "b.py" in res.stderr
    assert "@alice" in res.stderr
    assert "@bob" in res.stderr


# ── install-claude ─────────────────────────────────────────────────


def test_install_claude_writes_settings(tmp_path):
    """First-run install creates ``.claude/settings.json`` with the
    Spec-managed PreToolUse block."""
    from spec_cli.commands.hooks import install_claude_settings

    bundle = _make_bundle(tmp_path)
    path = install_claude_settings(bundle, block_mode=False)
    assert path.is_file()
    settings = json.loads(path.read_text(encoding="utf-8"))
    pre = settings["hooks"]["PreToolUse"]
    assert any(
        any(h.get("spec_managed") is True for h in entry.get("hooks", []))
        for entry in pre
    )


def test_install_claude_preserves_unrelated_entries(tmp_path):
    """User-authored hooks under PreToolUse must survive ``install-claude``.
    We replace only the Spec-managed entry, never the whole list.
    """
    from spec_cli.commands.hooks import install_claude_settings

    bundle = _make_bundle(tmp_path)
    settings_path = bundle / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps({
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "/usr/local/bin/my-bash-guard"}],
                    }
                ]
            },
            "permissions": {"allow": ["Read"]},
        }),
        encoding="utf-8",
    )
    install_claude_settings(bundle, block_mode=False)
    out = json.loads(settings_path.read_text(encoding="utf-8"))
    assert out["permissions"]["allow"] == ["Read"]
    pre = out["hooks"]["PreToolUse"]
    matchers = [e.get("matcher") for e in pre]
    assert "Bash" in matchers  # user's untouched
    assert any(
        m and "Edit" in m for m in matchers
    )  # ours appended


def test_install_claude_idempotent_on_re_run(tmp_path):
    """Re-running ``install-claude`` must not duplicate the
    Spec-managed block. Any existing Spec entry is replaced in
    place, not appended."""
    from spec_cli.commands.hooks import install_claude_settings

    bundle = _make_bundle(tmp_path)
    install_claude_settings(bundle, block_mode=False)
    install_claude_settings(bundle, block_mode=False)
    install_claude_settings(bundle, block_mode=True)  # mode flip
    path = bundle / ".claude" / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    pre = settings["hooks"]["PreToolUse"]
    spec_entries = [
        entry
        for entry in pre
        if any(h.get("spec_managed") is True for h in entry.get("hooks", []))
    ]
    assert len(spec_entries) == 1
    inner = spec_entries[0]["hooks"][0]
    assert "--block" in inner["command"]  # mode reflected
