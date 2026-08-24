"""
``spec hooks …`` — installable scripts that bridge Spec Live to AI IDEs.

Each command in this group is designed to be wired into another tool's
hook configuration and run in a fresh subprocess for every invocation.
That means:

* No assumptions about working directory — the AI IDE invokes them
  from whatever cwd it has.
* No long-lived in-memory state — everything we need to know lives in
  ``.spec/team-presence.json``.
* Failures are silent on stderr by default — a misbehaving hook should
  never block the user's work. The contract for blocking is the exit
  code; output is decoration.

The three surfaces that exist today:

* ``spec hooks claude-user-prompt`` — Claude Code ``UserPromptSubmit``
  hook. Prints the current coordination brief into the agent context.

* ``spec hooks claude-pre-tool-use`` — Claude Code ``PreToolUse``
  hook. Reads stdin (Claude's hook protocol), parses out the file
  path being edited, and warns when a teammate is currently editing
  it (using the same stale-mirror guard as ``spec locks check``).
  Exit 0 by default (warn-only); ``--block`` exits non-zero so
  Claude refuses to proceed without an explicit override.

* ``spec hooks install-claude`` — write the per-bundle ``.claude/
  settings.json`` so the above is wired into Claude Code without the
  user touching JSON. Idempotent: re-running updates the same block.

The Cursor / Codex / generic-LSP integrations are *not* in this group
because they don't take stdin from the AI IDE — Cursor reads
``.cursor/rules/spec-team-presence.md`` directly (provisioned by
``spec init``), and AGENTS.md tells any model-driven agent to invoke
``spec presence check`` voluntarily. See
``PROMPT-LIVE-PLAN.md`` §5 for the matrix.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from ..config import BundleNotFoundError, find_bundle_root
from ..realtime.active_edits import (
    DEFAULT_LOCK_TTL_SECS,
    ActiveEditsStore,
)
from ..realtime.conflicts import assess_path_conflict, resolve_coordination_path
from ..realtime.team_editing_brief import DEFAULT_LOCKS_MIRROR_STALE_SECS
from ..ui import dim

CLAUDE_HOOK_AGENT_ID = "claude_code"


CLAUDE_HOOK_VERSION = 2
CLAUDE_SETTINGS_DIR = ".claude"
CLAUDE_SETTINGS_FILENAME = "settings.json"
_COORDINATION_BRIEF_MAX_CHARS = 16_000


def _locks_max_mirror_age_secs() -> float:
    raw = os.environ.get("SPEC_LOCKS_MAX_MIRROR_AGE_SECS", "").strip()
    if not raw:
        return DEFAULT_LOCKS_MIRROR_STALE_SECS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_LOCKS_MIRROR_STALE_SECS


@click.group("hooks")
def hooks_group() -> None:
    """Spec Live hooks for AI IDEs.

    \b
    Subcommands:
      spec hooks claude-user-prompt   — inject current agent coordination
      spec hooks claude-pre-tool-use   — stdin-driven Claude Code hook
      spec hooks install-claude        — wire the hook into .claude/settings.json
    """


@hooks_group.command("claude-user-prompt")
def claude_user_prompt_cmd() -> None:
    """Inject the current Spec Live coordination brief into Claude Code.

    Missing bundle/file and read failures are silent no-ops. Hook output is
    bounded because Claude includes stdout in the prompt context.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError:
        return
    path = root / ".spec" / "team-coordination.md"
    try:
        if not path.is_file():
            return
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if not text:
        return
    if len(text) > _COORDINATION_BRIEF_MAX_CHARS:
        text = text[:_COORDINATION_BRIEF_MAX_CHARS].rstrip() + "\n[…truncated…]"
    click.echo(
        "Current Spec Live team coordination. Read before planning or "
        "editing; avoid duplicating active work.\n\n" + text
    )


# ── Claude Code PreToolUse hook ───────────────────────────────────────


@hooks_group.command("claude-pre-tool-use")
@click.option(
    "--block",
    "block_mode",
    is_flag=True,
    help=(
        "Exit non-zero (refusing the tool call) when a teammate is "
        "editing the target file. Default behaviour is warn-only "
        "(exit 0 with a stderr message) — friendlier for first-time "
        "users; opt in here if your team wants firm coordination."
    ),
)
def claude_pre_tool_use_cmd(block_mode: bool) -> None:
    """Claude Code ``PreToolUse`` hook entry point.

    Reads Claude's hook payload from stdin. When the tool being
    invoked targets a file a teammate is currently editing, prints a
    warning to stderr (Claude's UI surfaces stderr to the user) and
    optionally exits non-zero to block the call.

    The hook is intentionally tolerant: any parse failure, missing
    presence file, missing bundle, or unrelated tool name is a no-op
    (exit 0, silent). We never want this hook to be the reason an
    edit fails — that path is the user explicitly opting in via
    ``--block``.
    """
    raw = sys.stdin.read()
    if not raw.strip():
        # No input → nothing to check. Don't block.
        sys.exit(0)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        # Malformed input → fail open. Better to let an edit through
        # than to block on a Claude Code shape change we don't grok.
        sys.exit(0)
    if not isinstance(payload, dict):
        sys.exit(0)

    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    file_paths = _extract_file_paths(tool_name, tool_input)
    if not file_paths:
        sys.exit(0)

    # Find a bundle to consult. Prefer the cwd Claude is running from
    # (matches the user's intuition for "the project being edited"),
    # and fall back to the directory of the first edited file.
    bundle_root = _find_bundle_for_paths(file_paths)
    if bundle_root is None:
        sys.exit(0)
    conflicts: list[tuple[str, list[dict]]] = []
    unknown_paths: list[str] = []
    rel_paths: list[str] = []
    session_id = _claude_session_id(payload)
    for abs_path in file_paths:
        rel = _bundle_relative(abs_path, bundle_root)
        if rel is None:
            continue
        rel_paths.append(rel)
        assessment = assess_path_conflict(
            bundle_root,
            rel,
            include_active_edits=False,
            caller_agent=CLAUDE_HOOK_AGENT_ID,
            caller_session_id=session_id,
            max_presence_age_secs=_locks_max_mirror_age_secs(),
        )
        if assessment.state == "conflict":
            conflicts.append((rel, assessment.holders))
        elif assessment.state == "unknown":
            unknown_paths.append(rel)

    # ── single-user multi-agent lock ──────────────────────────────
    # Take an active-edit lock for this PreToolUse call so other
    # agents on the same machine (Cursor / Codex) see Claude's edit
    # in flight. The lock id is emitted on stderr (Claude shows
    # hook stderr inline) so the matching PostToolUse hook can
    # release it. Cross-agent overlaps are added to the conflict
    # list so we surface "your Cursor pane is also editing auth.py"
    # with the same warning channel as a teammate conflict.
    active_conflicts: list[dict] = []
    lock_id: str | None = None
    if rel_paths:
        try:
            store = ActiveEditsStore(bundle_root)
            lock, ac = store.acquire(
                rel_paths,
                agent=CLAUDE_HOOK_AGENT_ID,
                session_id=session_id,
                ttl_secs=_hook_lock_ttl_secs(),
                intent=tool_name or None,
            )
            lock_id = lock.id
            for c in ac:
                active_conflicts.append(
                    {
                        "agent": c.lock.agent,
                        "session_id": c.lock.session_id,
                        "intent": c.lock.intent,
                        "pid": c.lock.pid,
                        "overlapping_paths": list(c.overlapping_paths),
                        "expires_at": c.lock.expires_at.isoformat(),
                    }
                )
        except Exception:  # noqa: BLE001
            # Lock acquisition is best-effort. A broken store should
            # never block Claude from editing — the team-presence
            # warning still fires below.
            pass

    if lock_id:
        # Print the lock id on stderr in a stable form so the
        # PostToolUse hook can grep it back out. Claude's hook
        # protocol passes a session id to *both* hooks, so we
        # also fall back to "release every lock for this session"
        # if the post hook can't parse this line.
        sys.stderr.write(f"spec-lock-id: {lock_id}\n")
        sys.stderr.flush()

    if unknown_paths:
        sys.stderr.write(
            "⚠ Spec Live: coordination state unknown for "
            + ", ".join(unknown_paths)
            + "; start or repair `spec watch` before treating the path as clear.\n"
        )
        sys.stderr.flush()

    if not conflicts and not active_conflicts and not unknown_paths:
        sys.exit(0)

    if conflicts or active_conflicts:
        _emit_conflict_warning(tool_name, conflicts, active_conflicts)
    if block_mode:
        # Non-zero exit blocks the tool call in Claude Code.
        sys.exit(2 if conflicts or active_conflicts else 3)
    sys.exit(0)


def _extract_file_paths(tool_name: str, tool_input: dict) -> list[str]:
    """Pull every file-targeting argument out of a Claude tool call.

    Covers the tool names Claude Code uses for filesystem mutation
    today: ``Edit``, ``MultiEdit``, ``Write``, ``NotebookEdit``,
    ``StrReplace``, ``Delete``. Everything else (Bash, Grep, Read,
    web fetch, …) returns an empty list — we only care about
    edit-class tools, since reads and shell commands aren't where
    presence conflicts hurt.
    """
    edit_tools = {
        "Edit",
        "MultiEdit",
        "StrReplace",
        "Write",
        "NotebookEdit",
        "Delete",
    }
    if tool_name not in edit_tools:
        return []
    out: list[str] = []
    # Common: ``file_path`` (Edit, Write, Delete, MultiEdit).
    fp = tool_input.get("file_path")
    if isinstance(fp, str) and fp:
        out.append(fp)
    # NotebookEdit uses ``notebook_path``.
    np = tool_input.get("notebook_path")
    if isinstance(np, str) and np:
        out.append(np)
    # MultiEdit also accepts a list under ``edits``; rarely a file
    # list, but defensive.
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for e in edits:
            if isinstance(e, dict):
                p = e.get("file_path")
                if isinstance(p, str) and p:
                    out.append(p)
    # Deduplicate while preserving order.
    seen: set[str] = set()
    dedup: list[str] = []
    for p in out:
        if p in seen:
            continue
        seen.add(p)
        dedup.append(p)
    return dedup


def _find_bundle_for_paths(paths: list[str]) -> Path | None:
    """Resolve the coordination root for Claude's first editable path."""
    for p in paths:
        root, _ = resolve_coordination_path(p)
        if root is not None:
            return root
    return None


def _bundle_relative(abs_path: str, bundle_root: Path) -> str | None:
    try:
        p = Path(abs_path).resolve()
    except OSError:
        return None
    try:
        return str(p.relative_to(bundle_root.resolve()))
    except ValueError:
        return None


def _emit_conflict_warning(
    tool_name: str,
    conflicts: list[tuple[str, list[dict]]],
    active_conflicts: list[dict] | None = None,
) -> None:
    """Write the user-visible warning to stderr.

    Claude Code shows hook stderr inline with its tool-call output,
    which is exactly the surface we want — the user sees the warning
    next to the edit it's about to perform without us having to
    inject anything into the conversation.

    Two conflict layers are surfaced together:

    * ``conflicts`` — *teammate* dirty files from ``team-presence.json``.
      Cross-machine, cross-user; the original Spec Live warning.
    * ``active_conflicts`` — *your own other AI agents* on this
      machine that have the same file locked via
      ``~/.spec/active-edits.json``. Same-user, cross-tool, and shared
      across every local Spec repo. Surfaced
      *first* because a same-machine overlap is the more certain
      conflict — a teammate could be stale, your Cursor agent
      writing right now is not.
    """
    lines: list[str] = []
    active_conflicts = list(active_conflicts or [])

    if active_conflicts:
        lines.append(
            f"⚠ Spec Live: {len(active_conflicts)} of your own agents "
            f"have overlapping locks right now"
        )
        for entry in active_conflicts:
            agent = entry.get("agent") or "agent"
            session = entry.get("session_id") or "-"
            intent = entry.get("intent") or "-"
            paths = entry.get("overlapping_paths") or []
            paths_fmt = ", ".join(paths) if isinstance(paths, list) else ""
            lines.append(
                f"  · {agent} (session {session}, intent {intent}): {paths_fmt}"
            )
        lines.append(
            "  → wait for the other agent to finish, or call "
            "`spec locks list` to inspect and `spec locks release <id>`."
        )

    if conflicts:
        if len(conflicts) > 1:
            lines.append(
                f"⚠ Spec Live: {len(conflicts)} files have teammates editing them"
            )
        else:
            lines.append(
                "⚠ Spec Live: 1 file has a teammate currently editing it"
            )
        for rel, holders in conflicts:
            lines.append(f"  {rel}")
            for h in holders[:3]:
                handle = h.get("handle") or h.get("name") or "(unknown)"
                if h.get("kind") == "task_claim":
                    lines.append(
                        f"    · {handle} ({h.get('agent') or 'agent'}, "
                        f"session {h.get('session_id') or '-'}) — "
                        f"{h.get('objective') or 'active task claim'}"
                    )
                else:
                    added = int(h.get("lines_added") or 0)
                    removed = int(h.get("lines_removed") or 0)
                    untracked = " (new file)" if h.get("untracked") else ""
                    lines.append(
                        f"    · @{handle} (+{added}/-{removed}){untracked}"
                    )
            if len(holders) > 3:
                lines.append(f"    · …and {len(holders) - 3} more")
        lines.append(
            "  → consider `git pull` first or coordinate before overwriting."
        )

    if lines:
        sys.stderr.write("\n".join(lines) + "\n")
        sys.stderr.flush()


def _claude_session_id(payload: dict) -> str | None:
    """Pull the session identifier Claude Code passes to every hook.

    Claude's hook protocol includes a ``session_id`` field at the top
    level of the JSON payload. We use it as the active-edit lock's
    session id so re-firing the hook (e.g. a chain of Edit calls in
    one conversation) renews the same lock instead of stacking up
    overlapping ones. Falls back to ``None`` for unfamiliar payload
    shapes; the store still distinguishes renewals via the
    ``(agent, session_id=None)`` tuple.
    """
    raw = payload.get("session_id") or payload.get("sessionId")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _hook_lock_ttl_secs() -> float:
    """Hook lock TTL, overridable via ``SPEC_HOOK_LOCK_TTL_SECS``.

    Default is the module constant. Bumped on the cheap by setting
    the env var in ``.claude/settings.json`` for teams that run very
    long single-tool calls. Capped server-side in
    ``ActiveEditsStore.acquire`` regardless.
    """
    raw = os.environ.get("SPEC_HOOK_LOCK_TTL_SECS", "").strip()
    if not raw:
        return float(DEFAULT_LOCK_TTL_SECS)
    try:
        return float(raw)
    except ValueError:
        return float(DEFAULT_LOCK_TTL_SECS)


# ── Claude Code PostToolUse hook ──────────────────────────────────────


@hooks_group.command("claude-post-tool-use")
def claude_post_tool_use_cmd() -> None:
    """Claude Code ``PostToolUse`` hook entry point.

    Releases the active-edit lock(s) that the matching PreToolUse
    hook took for this tool call. Two release strategies, tried in
    order:

    * If the PreToolUse hook printed a ``spec-lock-id:`` line on
      stderr, Claude doesn't forward stderr between hooks — instead
      we use the ``session_id`` carried in both payloads to release
      every lock the *current Claude session* still holds. That
      catches the common case ("Edit, then MultiEdit, then Bash")
      where the PreToolUse hook renewed one cumulative lock per
      session.
    * As a safety net, we also accept ``SPEC_ACTIVE_LOCK_ID`` from
      the environment — a future custom integration could pass the
      id explicitly.

    Exit ``0`` regardless. A hook that fails to release is a
    leak the TTL will reclaim on its own; we never want a release
    failure to block work.
    """
    raw = sys.stdin.read()
    if not raw.strip():
        sys.exit(0)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        sys.exit(0)
    if not isinstance(payload, dict):
        sys.exit(0)

    session_id = _claude_session_id(payload)
    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    file_paths = _extract_file_paths(tool_name, tool_input)
    bundle_root = _find_bundle_for_paths(file_paths)
    if bundle_root is None:
        sys.exit(0)

    # Per-id release (if the env var carried it forward) plus
    # per-session sweep. We do both because the lock might have been
    # renewed under a different id between PreToolUse and now.
    explicit_id = os.environ.get("SPEC_ACTIVE_LOCK_ID", "").strip()
    try:
        store = ActiveEditsStore(bundle_root)
        if explicit_id:
            store.release(explicit_id)
        if session_id is not None:
            store.release_for_session(
                agent=CLAUDE_HOOK_AGENT_ID, session_id=session_id
            )
    except Exception:  # noqa: BLE001
        # Best-effort.
        pass
    sys.exit(0)


# ── Claude settings install ──────────────────────────────────────────


@hooks_group.command("install-claude")
@click.option(
    "--block",
    "block_mode",
    is_flag=True,
    help="Configure the hook in --block mode (refuses tool calls on conflict).",
)
def install_claude_cmd(block_mode: bool) -> None:
    """Write/refresh ``.claude/settings.json`` so Claude Code in this
    bundle runs the Spec Live PreToolUse hook on every edit.

    Idempotent: re-running updates the Spec-managed entry in place
    without touching unrelated settings the user added by hand.
    Removing the file (or the Spec-managed entry inside it) opts out.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    install_claude_settings(root, block_mode=block_mode)
    dim(f".claude/settings.json updated ({'block' if block_mode else 'warn'} mode)")


def install_claude_settings(bundle_root: Path, *, block_mode: bool) -> Path:
    """Programmatic variant of ``install-claude`` — used by ``spec
    init`` to wire the hook on first scaffold without spawning the
    CLI again. Returns the settings path.

    Schema written:

    .. code-block:: json

       {
         "hooks": {
           "PreToolUse": [
             {
               "matcher": "Edit|MultiEdit|Write|NotebookEdit",
               "hooks": [
                 {
                   "type": "command",
                   "command": "spec hooks claude-pre-tool-use",
                   "spec_managed": true,
                   "spec_version": 2
                 }
               ]
             }
           ],
           "UserPromptSubmit": [
             {
               "hooks": [
                 {
                   "type": "command",
                   "command": "spec live ensure --quiet && spec hooks claude-user-prompt",
                   "spec_managed": true,
                   "spec_version": 2
                 }
               ]
             }
           ]
         }
       }

    Two hooks today, both Spec-managed:

    * ``PreToolUse`` — warn (or block, with ``--block``) before Claude
      edits a file a teammate is currently in.
    * ``UserPromptSubmit`` — autostart the live watcher daemon and inject
      the current coordination brief before each Claude prompt.

    The ``spec_managed`` / ``spec_version`` markers are how we identify
    the entry on subsequent runs — anything else under ``hooks`` is
    left alone. Older versions get replaced; entries from Spec are
    deduplicated.
    """
    settings_dir = bundle_root / CLAUDE_SETTINGS_DIR
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / CLAUDE_SETTINGS_FILENAME

    existing: dict = {}
    if settings_path.is_file():
        try:
            parsed = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                existing = parsed
        except (OSError, ValueError):
            existing = {}

    hooks_section = existing.get("hooks")
    if not isinstance(hooks_section, dict):
        hooks_section = {}
        existing["hooks"] = hooks_section

    # ── PreToolUse: presence-warning hook ──────────────────────────
    pre_tool_use = hooks_section.get("PreToolUse")
    if not isinstance(pre_tool_use, list):
        pre_tool_use = []
        hooks_section["PreToolUse"] = pre_tool_use

    pre_command = "spec hooks claude-pre-tool-use"
    if block_mode:
        pre_command += " --block"

    pre_block = {
        "matcher": "Edit|MultiEdit|Write|NotebookEdit",
        "hooks": [
            {
                "type": "command",
                "command": pre_command,
                "spec_managed": True,
                "spec_version": CLAUDE_HOOK_VERSION,
            }
        ],
    }
    hooks_section["PreToolUse"] = _replace_spec_managed(pre_tool_use, pre_block)

    # ── PostToolUse: release the active-edit lock ──────────────────
    # Matches the PreToolUse matcher: the same tool calls that take
    # a lock should release it. Without this, a single-agent run is
    # fine (lock TTL reclaims it) but a Cursor pane checking
    # locks while Claude is between edits would see stale "in
    # flight" rows for up to 5 minutes.
    post_tool_use = hooks_section.get("PostToolUse")
    if not isinstance(post_tool_use, list):
        post_tool_use = []
        hooks_section["PostToolUse"] = post_tool_use
    post_block = {
        "matcher": "Edit|MultiEdit|Write|NotebookEdit",
        "hooks": [
            {
                "type": "command",
                "command": "spec hooks claude-post-tool-use",
                "spec_managed": True,
                "spec_version": CLAUDE_HOOK_VERSION,
            }
        ],
    }
    hooks_section["PostToolUse"] = _replace_spec_managed(post_tool_use, post_block)

    # ── UserPromptSubmit: autostart hook ───────────────────────────
    # The autostart command bails fast on opt-out / not-in-bundle —
    # quietly OK to fire from every Claude prompt. When Claude is
    # the user's only IDE (no terminal in front of them), this is
    # the path that actually starts the daemon for them.
    user_prompt_submit = hooks_section.get("UserPromptSubmit")
    if not isinstance(user_prompt_submit, list):
        user_prompt_submit = []
        hooks_section["UserPromptSubmit"] = user_prompt_submit

    autostart_block = {
        "hooks": [
            {
                "type": "command",
                "command": (
                    "spec live ensure --quiet && "
                    "spec hooks claude-user-prompt"
                ),
                "spec_managed": True,
                "spec_version": CLAUDE_HOOK_VERSION,
            }
        ],
    }
    hooks_section["UserPromptSubmit"] = _replace_spec_managed(
        user_prompt_submit, autostart_block
    )

    settings_path.write_text(
        json.dumps(existing, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return settings_path


def _replace_spec_managed(existing: list, new_block: dict) -> list:
    """Replace any Spec-managed entry in a Claude hook list, preserving
    everything else. Used by both ``PreToolUse`` and
    ``UserPromptSubmit`` slots so re-running ``install-claude``
    bumps Spec's blocks in place without touching user-authored ones.
    """
    pruned: list = []
    for entry in existing:
        if not isinstance(entry, dict):
            pruned.append(entry)
            continue
        inner = entry.get("hooks")
        if not isinstance(inner, list):
            pruned.append(entry)
            continue
        is_spec = any(
            isinstance(h, dict) and h.get("spec_managed") is True for h in inner
        )
        if not is_spec:
            pruned.append(entry)
    pruned.append(new_block)
    return pruned
