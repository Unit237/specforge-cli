"""Tests for the Cursor adapter.

We synthesise both halves of Cursor's storage layout in a tmp dir
(the per-workspace ``state.vscdb`` and the global one), then point
``CURSOR_HOME`` at it. No real Cursor install is required.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from spec_cli.sources.cursor import (
    cursor_global_storage_db,
    cursor_workspace_storage_root,
    read_cursor_sessions,
)


# ---------------------------------------------------------------------------
# Fake-store helpers
# ---------------------------------------------------------------------------


def _open_workspace_db(workspace_dir: Path) -> sqlite3.Connection:
    """Create the per-workspace ``state.vscdb`` schema Cursor uses."""
    workspace_dir.mkdir(parents=True, exist_ok=True)
    db_path = workspace_dir / "state.vscdb"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ItemTable "
        "(key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID"
    )
    return conn


def _open_global_db(global_dir: Path) -> sqlite3.Connection:
    """Create the global ``state.vscdb`` with the cursorDiskKV table."""
    global_dir.mkdir(parents=True, exist_ok=True)
    db_path = global_dir / "state.vscdb"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cursorDiskKV "
        "(key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID"
    )
    # Real Cursor installs also have ItemTable here, but the adapter
    # only reads cursorDiskKV from the global DB.
    return conn


def _make_workspace(
    cursor_home: Path,
    workspace_hash: str,
    folder: Path,
    composer_ids: list[str],
) -> Path:
    """Create a workspaceStorage entry pointing at ``folder`` with the
    given composer ids registered in ``composer.composerData``.

    Returns the workspace directory path.
    """
    workspace_dir = (
        cursor_home / "User" / "workspaceStorage" / workspace_hash
    )
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "workspace.json").write_text(
        json.dumps({"folder": f"file://{folder.resolve().as_posix()}"}),
        encoding="utf-8",
    )
    conn = _open_workspace_db(workspace_dir)
    composer_data = {
        "allComposers": [
            {
                "composerId": cid,
                "createdAt": 1_700_000_000_000 + i * 1000,
                "lastUpdatedAt": 1_700_000_000_000 + i * 1000 + 500,
            }
            for i, cid in enumerate(composer_ids)
        ],
    }
    conn.execute(
        "INSERT OR REPLACE INTO ItemTable VALUES (?, ?)",
        ("composer.composerData", json.dumps(composer_data)),
    )
    conn.commit()
    conn.close()
    return workspace_dir


def _add_composer(
    cursor_home: Path,
    composer_id: str,
    bubbles: list[dict],
    *,
    name: str | None = None,
    created_at_ms: int = 1_700_000_000_000,
    last_updated_ms: int = 1_700_000_000_500,
) -> None:
    """Write a composer's metadata + bubbles to the global DB.

    Each bubble is a dict with at minimum ``id``, ``type`` (1=user,
    2=assistant), and ``text`` keys; we serialise it into Cursor's
    on-disk shape (``bubbleId:<cid>:<bid>``) and link the bubbles via
    ``fullConversationHeadersOnly``.
    """
    global_dir = cursor_home / "User" / "globalStorage"
    conn = _open_global_db(global_dir)

    composer_data = {
        "_v": 13,
        "composerId": composer_id,
        "name": name,
        "createdAt": created_at_ms,
        "lastUpdatedAt": last_updated_ms,
        "fullConversationHeadersOnly": [
            {"bubbleId": b["id"], "type": b["type"]} for b in bubbles
        ],
    }
    conn.execute(
        "INSERT OR REPLACE INTO cursorDiskKV VALUES (?, ?)",
        (f"composerData:{composer_id}", json.dumps(composer_data)),
    )

    for bubble in bubbles:
        body = {
            "_v": 3,
            "bubbleId": bubble["id"],
            "type": bubble["type"],
            "text": bubble.get("text", ""),
            "createdAt": bubble.get("createdAt", "2026-03-10T11:00:00Z"),
        }
        if "modelInfo" in bubble:
            body["modelInfo"] = bubble["modelInfo"]
        conn.execute(
            "INSERT OR REPLACE INTO cursorDiskKV VALUES (?, ?)",
            (f"bubbleId:{composer_id}:{bubble['id']}", json.dumps(body)),
        )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cursor_paths_resolve_under_cursor_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_HOME", str(tmp_path))
    assert cursor_workspace_storage_root() == tmp_path / "User" / "workspaceStorage"
    assert (
        cursor_global_storage_db()
        == tmp_path / "User" / "globalStorage" / "state.vscdb"
    )


def test_read_cursor_sessions_extracts_user_and_assistant_turns(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_HOME", str(tmp_path))
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    composer_id = "11111111-1111-4111-a111-111111111111"
    _make_workspace(tmp_path, "ws1", bundle, [composer_id])
    _add_composer(
        tmp_path,
        composer_id,
        name="Refactor billing.py",
        bubbles=[
            {
                "id": "bub-1",
                "type": 1,
                "text": "Refactor billing.py please.",
                "createdAt": "2026-03-10T11:00:00Z",
            },
            {
                "id": "bub-2",
                "type": 2,
                "text": "Sure — scanning call sites first.",
                "createdAt": "2026-03-10T11:00:30Z",
                "modelInfo": {"modelName": "claude-sonnet-4-5"},
            },
        ],
    )

    sessions = list(read_cursor_sessions(bundle))
    assert len(sessions) == 1
    s = sessions[0]
    assert s.id == composer_id
    assert s.source == "cursor"
    assert s.title == "Refactor billing.py"
    assert s.model == "claude-sonnet-4-5"
    assert s.cwd == str(bundle.resolve())
    assert len(s.turns) == 2
    assert s.turns[0].role == "user"
    assert s.turns[0].text == "Refactor billing.py please."
    assert s.turns[1].role == "assistant"
    assert s.turns[1].text is None  # non-verbose
    assert s.turns[1].summary is not None
    assert "scanning" in s.turns[1].summary.lower()


def test_read_cursor_sessions_returns_empty_when_store_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_HOME", str(tmp_path / "nope"))
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    assert list(read_cursor_sessions(bundle)) == []


def test_read_cursor_sessions_ignores_workspaces_outside_bundle(tmp_path, monkeypatch):
    """A composer logged in a different workspace must not leak in."""
    monkeypatch.setenv("CURSOR_HOME", str(tmp_path))

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()

    bundle_composer = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
    other_composer = "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"

    _make_workspace(tmp_path, "wsA", bundle, [bundle_composer])
    _make_workspace(tmp_path, "wsB", other, [other_composer])

    _add_composer(
        tmp_path,
        bundle_composer,
        bubbles=[{"id": "u1", "type": 1, "text": "in the bundle"}],
    )
    _add_composer(
        tmp_path,
        other_composer,
        bubbles=[{"id": "u1", "type": 1, "text": "outside"}],
    )

    sessions = list(read_cursor_sessions(bundle))
    assert [s.id for s in sessions] == [bundle_composer]


def test_read_cursor_sessions_includes_subworkspaces(tmp_path, monkeypatch):
    """Cursor opened on `<bundle>/backend` should still attach to the bundle.

    Mirrors the git-style "any subdir of the worktree counts" rule we
    enforce in the Claude Code adapter.
    """
    monkeypatch.setenv("CURSOR_HOME", str(tmp_path))

    bundle = tmp_path / "bundle"
    sub = bundle / "backend"
    sub.mkdir(parents=True)

    root_composer = "11111111-1111-4111-a111-111111111111"
    sub_composer = "22222222-2222-4222-a222-222222222222"

    _make_workspace(tmp_path, "ws-root", bundle, [root_composer])
    _make_workspace(tmp_path, "ws-sub", sub, [sub_composer])
    for cid in (root_composer, sub_composer):
        _add_composer(
            tmp_path,
            cid,
            bubbles=[{"id": "u1", "type": 1, "text": "hi"}],
        )

    sessions = list(read_cursor_sessions(bundle))
    assert sorted(s.id for s in sessions) == sorted([root_composer, sub_composer])


def test_read_cursor_sessions_includes_ancestor_workspaces(tmp_path, monkeypatch):
    """Cursor opened on the parent monorepo must still attach the
    composer to a bundle living in a child directory.

    The common shape: developer keeps Cursor open on
    ``~/Code/megarepo`` and runs ``spec init`` inside
    ``~/Code/megarepo/services/billing``. Without ancestor matching,
    every Cursor prompt typed about the billing bundle would be lost
    — the workspace folder would never equal or descend from the
    bundle root.
    """
    monkeypatch.setenv("CURSOR_HOME", str(tmp_path))

    parent = tmp_path / "monorepo"
    bundle = parent / "services" / "billing"
    bundle.mkdir(parents=True)

    composer = "cccccccc-cccc-4ccc-aaaa-cccccccccccc"
    _make_workspace(tmp_path, "ws-parent", parent, [composer])
    _add_composer(
        tmp_path,
        composer,
        bubbles=[{"id": "u1", "type": 1, "text": "tweak the billing bundle"}],
    )

    sessions = list(read_cursor_sessions(bundle))
    assert [s.id for s in sessions] == [composer]
    # Anchored at the bundle root, not the wider monorepo folder —
    # `Session.cwd` represents which bundle the conversation belongs
    # to, and that's the bundle we discovered, not the umbrella.
    assert sessions[0].cwd == str(bundle.resolve())


def test_read_cursor_sessions_drops_empty_user_bubbles(tmp_path, monkeypatch):
    """Empty user bubbles (just attachments, no text) would fail the
    schema's `user turn requires text` rule — drop them up front."""
    monkeypatch.setenv("CURSOR_HOME", str(tmp_path))

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    composer_id = "33333333-3333-4333-a333-333333333333"
    _make_workspace(tmp_path, "ws", bundle, [composer_id])
    _add_composer(
        tmp_path,
        composer_id,
        bubbles=[
            {"id": "u-empty", "type": 1, "text": "   "},  # whitespace only
            {"id": "u-real", "type": 1, "text": "actually a question"},
            {"id": "a-real", "type": 2, "text": "answer."},
        ],
    )

    sessions = list(read_cursor_sessions(bundle))
    assert len(sessions) == 1
    roles = [t.role for t in sessions[0].turns]
    assert roles == ["user", "assistant"]
    assert sessions[0].turns[0].text == "actually a question"


def test_read_cursor_sessions_verbose_keeps_assistant_text(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_HOME", str(tmp_path))
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    cid = "44444444-4444-4444-a444-444444444444"
    _make_workspace(tmp_path, "ws-verbose", bundle, [cid])
    _add_composer(
        tmp_path,
        cid,
        bubbles=[
            {"id": "u", "type": 1, "text": "hi"},
            {
                "id": "a",
                "type": 2,
                "text": "A longer assistant reply that we want to keep verbatim.",
            },
        ],
    )

    sessions = list(read_cursor_sessions(bundle, verbose=True))
    assert sessions[0].verbose is True
    # Verbose turns DO carry text; the schema otherwise forbids it.
    assert sessions[0].turns[1].text is not None


def test_read_cursor_sessions_finds_sessions_under_alias(tmp_path, monkeypatch):
    """Fix #2 (rename) for Cursor: passing the historical-paths list
    finds composers logged under a previous bundle location."""
    monkeypatch.setenv("CURSOR_HOME", str(tmp_path))

    old_path = tmp_path / "billing"
    old_path.mkdir()
    cid = "55555555-5555-4555-a555-555555555555"
    _make_workspace(tmp_path, "ws-old", old_path, [cid])
    _add_composer(
        tmp_path,
        cid,
        bubbles=[{"id": "u", "type": 1, "text": "before the rename"}],
    )

    new_path = tmp_path / "payments"
    shutil.move(str(old_path), str(new_path))
    # Note: workspace.json under ws-old still points at the OLD path,
    # which no longer exists. With only the new path in scope, the
    # adapter doesn't find the session.
    assert list(read_cursor_sessions(new_path)) == []
    # With the alias list, Cursor's workspace.json folder still resolves
    # under one of the recorded roots, so the session reappears.
    sessions = list(read_cursor_sessions([new_path, old_path]))
    assert [s.id for s in sessions] == [cid]


def test_read_cursor_sessions_skips_non_file_workspaces(tmp_path, monkeypatch):
    """Multi-root and remote (e.g. ssh://) workspaces aren't local
    bundles — must not be scanned."""
    monkeypatch.setenv("CURSOR_HOME", str(tmp_path))

    bundle = tmp_path / "bundle"
    bundle.mkdir()

    # Remote workspace pointing into the bundle — must still be skipped.
    remote_dir = tmp_path / "User" / "workspaceStorage" / "ws-remote"
    remote_dir.mkdir(parents=True)
    (remote_dir / "workspace.json").write_text(
        json.dumps({"folder": f"vscode-remote://ssh-remote+host{bundle.as_posix()}"}),
        encoding="utf-8",
    )

    # Multi-root config — also skipped (we only handle single-folder
    # bundles in v0.1).
    multi_dir = tmp_path / "User" / "workspaceStorage" / "ws-multi"
    multi_dir.mkdir(parents=True)
    (multi_dir / "workspace.json").write_text(
        json.dumps({"configuration": "file:///somewhere/multi.code-workspace"}),
        encoding="utf-8",
    )

    assert list(read_cursor_sessions(bundle)) == []
