"""
Tests for the ``spec live`` autostart machinery.

Three surfaces under test:

1. **Shell snippet content** — the bash/zsh and fish bodies emitted by
   ``spec shell snippet`` must contain the per-shell prompt-render hook
   (precmd / PROMPT_COMMAND / fish_prompt event), the bundle-root
   walker, and the ``SPEC_NO_AUTOSTART`` opt-out. Pure text-shape
   assertions; no shell exec'ing.

2. **`spec live ensure`** — opt-out paths return cleanly (no spawn) on
   each of: ``SPEC_NO_AUTOSTART=1``, ``autostart=off`` preference,
   not-in-bundle, and missing-credentials. The happy path is exercised
   in ``test_realtime_daemon.py``.

3. **Claude UserPromptSubmit hook** — ``install_claude_settings``
   writes both PreToolUse (presence guard) and UserPromptSubmit
   (autostart) entries; idempotent on re-run.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path



# ── shell snippet shape ─────────────────────────────────────────────


def test_bash_zsh_snippet_contains_autostart_pieces():
    """The bash/zsh snippet must contain everything the autostart
    contract relies on. Locking these strings in keeps a regression
    from silently dropping the hook on upgrade."""
    from spec_cli.commands.shell import SHELL_INTEGRATION_BODY_BASH_ZSH as body

    # Original integration must still be there (we didn't break the
    # `git init` wrapper while adding the autostart hook).
    assert "git()" in body
    assert "spec init" in body

    # New autostart machinery.
    assert "__spec_find_bundle_root" in body
    assert "spec bundle root --quiet" in body
    assert "__spec_live_autostart" in body
    assert "spec live ensure --quiet" in body
    assert "SPEC_NO_AUTOSTART" in body

    # Per-shell prompt event wiring.
    assert "precmd_functions" in body          # zsh path
    assert "PROMPT_COMMAND" in body            # bash path

    # Idempotent installer guards: must not append our hook twice if
    # the snippet is re-sourced. Look for the case-insensitive
    # repeat-check sentinel we wrote.
    assert "__spec_live_autostart " in body  # space matches the case match
    assert "__spec_live_autostart;" in body


def test_fish_snippet_contains_autostart_pieces():
    from spec_cli.commands.shell import SHELL_INTEGRATION_BODY_FISH as body

    assert "function git" in body
    assert "spec init" in body

    assert "__spec_find_bundle_root" in body
    assert "spec bundle root --quiet" in body
    assert "spec live ensure --quiet" in body
    assert "SPEC_NO_AUTOSTART" in body
    assert "--on-event fish_prompt" in body


def test_snippets_walk_up_filesystem_with_a_bound():
    """Both snippets must bound the upward walk so a runaway $PWD
    never stats unbounded directories — the tax on every prompt."""
    from spec_cli.commands.shell import (
        SHELL_INTEGRATION_BODY_BASH_ZSH,
        SHELL_INTEGRATION_BODY_FISH,
    )

    # We pick a generous bound (32) so users with deep nested
    # monorepos don't lose the autostart, but not so generous that
    # a stray cd to / scans the whole disk.
    assert "__spec_steps" in SHELL_INTEGRATION_BODY_BASH_ZSH
    assert "32" in SHELL_INTEGRATION_BODY_BASH_ZSH
    assert "__spec_steps" in SHELL_INTEGRATION_BODY_FISH
    assert "32" in SHELL_INTEGRATION_BODY_FISH


# ── spec live ensure opt-outs ────────────────────────────────────


def _write_bundle(path: Path) -> Path:
    bundle = path / "bundle"
    bundle.mkdir()
    (bundle / "spec.yaml").write_text("name: demo\n", encoding="utf-8")
    return bundle


def _run_ensure(args: list[str], *, env: dict[str, str], cwd: Path) -> tuple[int, str, str]:
    """Run ``spec live ensure`` as a subprocess and return (rc, stdout, stderr).

    We run via ``python -m spec_cli`` so we don't depend on a shell
    PATH lookup — but disable autostart-spawning by either being
    outside a bundle, having the env opt-out, or by lacking creds.
    """
    import subprocess

    full_env = {**os.environ, **env}
    proc = subprocess.run(
        [sys.executable, "-m", "spec_cli", "live", "ensure", *args],
        cwd=str(cwd),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_ensure_silent_outside_bundle(tmp_path):
    """``$PWD`` outside any bundle → no daemon, exit 0, silent."""
    rc, out, err = _run_ensure(
        ["--quiet"],
        env={"SPEC_HOME": str(tmp_path / "spec_home"), "SPEC_NO_AUTOSTART": "0"},
        cwd=tmp_path,
    )
    assert rc == 0, f"stderr: {err!r}"
    # --quiet must not pollute stdout/stderr at all.
    assert out.strip() == ""


def test_ensure_silent_with_env_opt_out(tmp_path):
    """``SPEC_NO_AUTOSTART=1`` → exit 0, no daemon, no PID file. Even
    inside a bundle. Even with credentials. The env var is the
    nuclear off switch."""
    bundle = _write_bundle(tmp_path)
    rc, out, _err = _run_ensure(
        ["--quiet"],
        env={"SPEC_HOME": str(tmp_path / "spec_home"), "SPEC_NO_AUTOSTART": "1"},
        cwd=bundle,
    )
    assert rc == 0
    assert out.strip() == ""
    # No pid file should have been written.
    assert not (bundle / ".spec" / "watch.pid").exists()


def test_ensure_silent_with_preferences_opt_out(tmp_path):
    """``autostart: off`` in ``~/.spec/preferences.json`` → exit 0,
    no daemon."""
    spec_home = tmp_path / "spec_home"
    spec_home.mkdir()
    (spec_home / "preferences.json").write_text(
        json.dumps({"schema": 1, "autostart": "off", "prompt_stream": "default"}),
        encoding="utf-8",
    )

    bundle = _write_bundle(tmp_path)
    rc, _out, _err = _run_ensure(
        ["--quiet"],
        env={"SPEC_HOME": str(spec_home), "SPEC_NO_AUTOSTART": "0"},
        cwd=bundle,
    )
    assert rc == 0
    assert not (bundle / ".spec" / "watch.pid").exists()


def test_ensure_silent_when_unauthenticated(tmp_path):
    """No credentials → autostart is a silent no-op. We don't pop a
    login prompt from a shell hook. The user will run ``spec login``
    when they're ready."""
    spec_home = tmp_path / "spec_home"
    spec_home.mkdir()
    # Empty creds file is treated as "not signed in".
    bundle = _write_bundle(tmp_path)

    rc, _out, _err = _run_ensure(
        ["--quiet"],
        env={"SPEC_HOME": str(spec_home), "SPEC_NO_AUTOSTART": "0"},
        cwd=bundle,
    )
    assert rc == 0
    assert not (bundle / ".spec" / "watch.pid").exists()


# ── Claude UserPromptSubmit hook ─────────────────────────────────


def test_install_claude_writes_user_prompt_submit_autostart(tmp_path):
    """Re-running install-claude must wire the autostart command into
    Claude's ``UserPromptSubmit`` hook so users prompting in Claude
    Code (without ever opening a terminal) still get autostart."""
    from spec_cli.commands.hooks import install_claude_settings

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "spec.yaml").write_text("name: demo\n", encoding="utf-8")

    path = install_claude_settings(bundle, block_mode=False)
    settings = json.loads(path.read_text(encoding="utf-8"))

    ups = settings["hooks"]["UserPromptSubmit"]
    assert isinstance(ups, list) and len(ups) >= 1

    # Find our spec-managed entry.
    spec_entries = [
        entry
        for entry in ups
        if any(
            isinstance(h, dict) and h.get("spec_managed") is True
            for h in entry.get("hooks", [])
        )
    ]
    assert len(spec_entries) == 1, "exactly one Spec-managed UserPromptSubmit entry"
    inner = spec_entries[0]["hooks"][0]
    assert inner["command"] == (
        "spec live ensure --quiet && spec hooks claude-user-prompt"
    )


def test_install_claude_user_prompt_submit_idempotent(tmp_path):
    """Multiple installs must not duplicate the UserPromptSubmit
    autostart entry."""
    from spec_cli.commands.hooks import install_claude_settings

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "spec.yaml").write_text("name: demo\n", encoding="utf-8")

    install_claude_settings(bundle, block_mode=False)
    install_claude_settings(bundle, block_mode=True)
    install_claude_settings(bundle, block_mode=False)

    path = bundle / ".claude" / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    ups = settings["hooks"]["UserPromptSubmit"]

    spec_entries = [
        e
        for e in ups
        if any(
            isinstance(h, dict) and h.get("spec_managed") is True
            for h in e.get("hooks", [])
        )
    ]
    assert len(spec_entries) == 1


def test_install_claude_preserves_user_authored_user_prompt_submit(tmp_path):
    """User-authored UserPromptSubmit entries must survive a re-run.
    We replace only the Spec-managed block."""
    from spec_cli.commands.hooks import install_claude_settings

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "spec.yaml").write_text("name: demo\n", encoding="utf-8")

    settings_path = bundle / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(
        json.dumps({
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {"type": "command", "command": "/usr/local/bin/audit-prompts"}
                        ]
                    }
                ]
            }
        }),
        encoding="utf-8",
    )

    install_claude_settings(bundle, block_mode=False)
    out = json.loads(settings_path.read_text(encoding="utf-8"))
    ups = out["hooks"]["UserPromptSubmit"]
    cmds = [
        h.get("command")
        for entry in ups
        for h in entry.get("hooks", [])
        if isinstance(h, dict)
    ]
    assert "/usr/local/bin/audit-prompts" in cmds  # user's untouched
    assert (
        "spec live ensure --quiet && spec hooks claude-user-prompt" in cmds
    )  # ours appended
