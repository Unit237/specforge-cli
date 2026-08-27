"""`spec init` — scaffold a new bundle in the current directory."""

from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import click

from ..config import BundleNotFoundError, find_bundle_root
from ..constants import MANIFEST_FILENAME, PROMPTS_DIRNAME
from ..git import find_git_dir, repo_name_from_remote_url
from ..preferences import remember_bundle
from ..sources.claude_code import claude_code_store_root
from ..ui import dim, fatal, info, ok, pointer


AGENTS_FILENAME: str = "AGENTS.md"

AGENTS_COORDINATION_BLOCK_BEGIN: str = "<!-- >>> spec live coordination >>>"
AGENTS_COORDINATION_BLOCK_END: str = "<!-- <<< spec live coordination <<< -->"
AGENTS_COORDINATION_BLOCK_BODY: str = f"""\
{AGENTS_COORDINATION_BLOCK_BEGIN}
## Spec Live — multi-agent coordination

This bundle uses **Spec Live** to coordinate coding agents working in parallel.

Before planning or editing:

1. Run `spec status` to verify this machine's workday switch and watchers.
   Read `.spec/team-coordination.md` when it exists; the brief lists active
   objectives, progress, claimed paths, and recent handoffs.
2. Do not duplicate an active objective. Split the work, wait for the handoff,
   or tell the user about the overlap.
3. Before modifying an existing or potentially shared path, run
   `spec locks check <bundle-relative-path>`. Exit `0` means clear, exit `2`
   means another agent may be editing it, and exit `3` means coordination
   health is unknown. Surface either non-zero result before proceeding.
4. Report material progress, paths changed, blockers, and the final outcome in
   normal assistant messages. Spec Live shares those updates automatically.
5. When the user asks for cloud review of the current pull request, run
   `spec review`. Do not assume a reviewer was requested merely because a PR
   exists; the explicit `agent-review` label is the authorization trigger.

Treat the coordination brief as advisory and the lock check as the mechanical
conflict signal. Commentary bubbles keep a round active; the brief disappears
after the final answer or expiry. When Spec is OFF or a watcher is stopped,
lock checks return unknown rather than claiming the path is clear. Only the
human operator should change the workday switch with `spec on` / `spec off`.
Never hand-edit files under `.spec/`.
{AGENTS_COORDINATION_BLOCK_END}
"""


# `.gitignore` block — Spec-managed, idempotent. Re-running `spec init`
# replaces the block in place via these sentinels; deleting both
# sentinels opts out (the block won't be reinstalled). We deliberately
# duplicate the well-known ``.spec/`` line even though ``.spec/.gitignore``
# already self-ignores: a single top-level ``.gitignore`` is what
# engineers reviewing the repo expect to see, and the redundancy is
# harmless — git takes the union.
GITIGNORE_BLOCK_BEGIN: str = "# >>> spec >>>"
GITIGNORE_BLOCK_END: str = "# <<< spec <<<"
GITIGNORE_BLOCK_BODY: str = f"""\
{GITIGNORE_BLOCK_BEGIN}
# Auto-managed by `spec init`. Re-run to update; or delete the whole
# block (sentinels included) to opt out.
.spec/         # Spec CLI's local index/staging directory.
out/           # Default `spec compile` output target — regenerated.
.claude/settings.local.json   # Claude Code per-user settings — never shared.
{GITIGNORE_BLOCK_END}
"""

# Git hooks installed under `.git/hooks/`. Each block is non-destructive:
# re-running `spec init` replaces only the Spec-managed segment between
# the sentinels. `--skip-git-hook` skips all three.

PRE_COMMIT_HOOK_BEGIN: str = "# >>> spec pre-commit >>>"
PRE_COMMIT_HOOK_END: str = "# <<< spec pre-commit <<<"
PRE_COMMIT_HOOK_BODY: str = f"""\
{PRE_COMMIT_HOOK_BEGIN}
# Auto-installed by `spec init`. Runs before git locks the tree for the
# pending commit: captures any new Cursor / Claude Code sessions into
# `prompts/<branch>.prompts`, `git add`s the file so it ships in the
# SAME commit (no follow-up commit needed), then mirrors paths you
# `git add`-ed into `spec add` (and removals into `spec unstage`) so
# spec staging tracks the same bundle files as git. Never blocks the
# commit — failures are swallowed per line below.
if command -v spec >/dev/null 2>&1; then
  spec git-hooks pre-commit || true
else
  echo "spec: CLI not on PATH; skipping prompts capture + spec/git index sync." >&2
fi
{PRE_COMMIT_HOOK_END}
"""

POST_COMMIT_HOOK_BEGIN: str = "# >>> spec post-commit >>>"
POST_COMMIT_HOOK_END: str = "# <<< spec post-commit <<<"
POST_COMMIT_HOOK_BODY: str = f"""\
{POST_COMMIT_HOOK_BEGIN}
# Deprecated: prompts capture moved to the commit-msg hook so `.prompts`
# updates are staged into the same git commit. This block is left behind only
# so `spec git-hooks install` can retire older post-commit scripts.
{POST_COMMIT_HOOK_END}
"""

COMMIT_MSG_HOOK_BEGIN: str = "# >>> spec commit-msg >>>"
COMMIT_MSG_HOOK_END: str = "# <<< spec commit-msg <<<"
COMMIT_MSG_HOOK_BODY: str = f"""\
{COMMIT_MSG_HOOK_BEGIN}
# Deprecated: prompts capture moved to the pre-commit hook so the
# resulting `.prompts` file lands in the SAME commit as the rest of
# the user's changes. `commit-msg` runs after git locks the cache_tree,
# so any modifications + `git add`s here update the index file but git
# ignores them for this commit (the tree is already built). Block left
# in place so existing installs replace it cleanly on `spec git-hooks
# install`; new installs may eventually drop it entirely.
{COMMIT_MSG_HOOK_END}
"""

PRE_PUSH_HOOK_BEGIN: str = "# >>> spec pre-push >>>"
PRE_PUSH_HOOK_END: str = "# <<< spec pre-push <<<"
PRE_PUSH_HOOK_BODY: str = f"""\
{PRE_PUSH_HOOK_BEGIN}
# Auto-installed by `spec init`. Runs during `git push` for branch refs
# so `spec push` runs in lockstep with git (same branch + SHA).
# Skip with: SKIP_SPEC_PUSH=1 or git push --no-verify
if [ "${{SKIP_SPEC_PUSH:-}}" != "1" ]; then
  if command -v spec >/dev/null 2>&1; then
    spec git-hooks pre-push || exit 1
  else
    echo "spec: CLI not on PATH; skipping spec push." >&2
  fi
fi
{PRE_PUSH_HOOK_END}
"""

POST_MERGE_HOOK_BEGIN: str = "# >>> spec post-merge >>>"
POST_MERGE_HOOK_END: str = "# <<< spec post-merge <<<"
POST_MERGE_HOOK_BODY: str = f"""\
{POST_MERGE_HOOK_BEGIN}
# Auto-installed by `spec init`. Fires after `git merge` (and the merge
# half of `git pull`) succeeds. When the user is on the trunk branch,
# rolls every `prompts/<slug>.prompts` from a non-trunk branch into
# `prompts/<trunk>.prompts` and `git add`s the result so the rollup
# shows up in `git status` for a follow-up commit. Then runs a fast
# local-only `spec bundle doctor` (stderr hints only when misaligned).
# Never blocks.
if command -v spec >/dev/null 2>&1; then
  spec git-hooks post-merge || true
else
  echo "spec: CLI not on PATH; skipping prompts rollup." >&2
fi
{POST_MERGE_HOOK_END}
"""

# Rows for `_install_git_hook_segment` — shared with `spec git-hooks install`.
GIT_HOOK_INSTALL_ROWS: list[tuple[str, str, str, str, str, str]] = [
    (
        "pre-commit",
        "pre-commit",
        PRE_COMMIT_HOOK_BEGIN,
        PRE_COMMIT_HOOK_END,
        PRE_COMMIT_HOOK_BODY,
        "#!/bin/sh\n\n",
    ),
    (
        "commit-msg",
        "commit-msg",
        COMMIT_MSG_HOOK_BEGIN,
        COMMIT_MSG_HOOK_END,
        COMMIT_MSG_HOOK_BODY,
        "#!/bin/sh\n\n",
    ),
    (
        "post-commit",
        "post-commit",
        POST_COMMIT_HOOK_BEGIN,
        POST_COMMIT_HOOK_END,
        POST_COMMIT_HOOK_BODY,
        "#!/bin/sh\n\n",
    ),
    (
        "pre-push",
        "pre-push",
        PRE_PUSH_HOOK_BEGIN,
        PRE_PUSH_HOOK_END,
        PRE_PUSH_HOOK_BODY,
        "#!/bin/sh\nset -e\n\n",
    ),
    (
        "post-merge",
        "post-merge",
        POST_MERGE_HOOK_BEGIN,
        POST_MERGE_HOOK_END,
        POST_MERGE_HOOK_BODY,
        "#!/bin/sh\n\n",
    ),
]


_STARTER_DOC = """# Product

> One-line description of what this bundle is.

## Goals

- What it does
- Who it's for
- What "done" looks like

## Non-goals

- Anything we're deliberately not building

## Behavior

Describe behavior in plain English. The compiler will read this file first.
"""


_STARTER_PROMPTS_TEMPLATE = """\
schema = "spec.prompts/v0.1"

# Starter `.prompts` file written by `spec init`.
#
# One `.prompts` file == one commit. Inside, each `[[sessions]]` block is
# one conversation that contributed to this commit — there can be many.
# `spec prompts capture` (run automatically from the commit-msg git hook
# installed by `spec init`) appends new captured sessions here; you can
# also hand-edit this
# file to rewrite history (title / summary / lesson / outcome) before
# pushing. The compiler routes each session to the LLM pinned in
# `model` below.
#
# Feel free to delete this template once you have real captured
# sessions, or keep it and edit in place — the file format is stable.

[commit]
branch          = "main"
message         = "Bundle scaffolded by `spec init`"
committed_at    = {committed_at}
author_name     = "{author_name}"
author_email    = "{author_email}"

[[sessions]]
id          = "{session_id}"
source      = "manual"
model       = "claude-sonnet-4-5"
title       = "Why this bundle exists"
summary     = '''
Replace this with the story of what the bundle is for. One paragraph.
Reviewers will read this before the spec doc — treat it as the README
of the conversation trail.
'''
lesson      = "Every `[[sessions]]` block should teach the next reviewer something that wasn't obvious."
tags        = ["scaffold"]
outcome     = "shipped"
visibility  = "public"

  [[sessions.turns]]
  role = "user"
  text = '''
  Describe the first thing you asked the AI about this bundle. The
  compiler will see this verbatim; keep it concise and high-signal.
  '''

  [[sessions.turns]]
  role    = "assistant"
  summary = "One-line description of what the AI produced in this turn."
"""


# We emit the manifest as text (not via yaml.safe_dump) so we can keep an
# illustrative, commented-out `routes:` block. The whole point of the route
# table is that users should learn it by osmosis; a silent empty list in the
# scaffold wouldn't teach anything.
#
# Defaults target Claude via Anthropic — the compile path users are most
# likely to hit (either through Claude Code directly, or via `--via api`).
_STARTER_MANIFEST = """# spec.yaml — bundle manifest
schema: "spec/v0.1"
name: {name}
description: ""

spec:
  entry: docs/product.md
  include:
    - "docs/**/*.md"
  exclude: []

compiler:
  # defaults — used when no route matches and no frontmatter overrides.
  # Aligned with the Claude-Code-first workflow; `spec compile` writes
  # a prompt your running Claude Code session will pick up. `--via api`
  # routes through the same model.
  engine: anthropic
  model: claude-sonnet-4-5
  temperature: 0.2
  max_output_tokens: 8000

  # route table — first match wins. uncomment to route different docs to
  # different models. any compiler.* key can be overridden per route.
  # routes:
  #   - match: "docs/architecture/**/*.md"
  #     model: claude-opus-4
  #     temperature: 0.15

output:
  target: ./out
  changelog: true
  commit_style: conventional

approvals:
  required: 1

cloud:
  project: {cloud_project}
  # Prefer `handle/slug` (matches Cloud URLs) so teammates resolve the
  # same bundle without relying on who is logged in. When `spec init`
  # runs after `spec login`, we stamp your handle + bundle name; otherwise
  # this is a bare slug until the first `spec push` canonicalizes it.
  # `bundle_id:` is stamped on the first successful `spec push` (PLAN.md §11).
  # Once set, every push verifies it against the remote — pointing
  # `cloud.project` at an unrelated bundle by accident is then a hard refusal.

  # Spec Live — real-time prompt sharing across the team. Each new
  # turn in any local Cursor / Codex / Claude Code / Compress session is
  # redacted, posted to Spec Cloud, and fanned out via SSE to every
  # teammate's `spec watch` daemon. Enabled by default — the team
  # feed lights up the moment a teammate installs the CLI. Toggle
  # with `spec live on` / `spec live off`, or per-machine with
  # `spec live mute`. See `spec/PROMPT-LIVE-PLAN.md`.
  prompt_stream:
    enabled: true
    # `verbose: true` would also share assistant *full text* (not
    # just summaries) — off by default since assistant bodies are
    # big and often sensitive.
"""


# Cursor rule scaffold. Cursor reads `.cursor/rules/*.mdc` (or `.md`) in
# the project root and surfaces them to the model as system-level
# guidance. We drop a Spec-Live-aware rule so Cursor checks
# ``.spec/team-presence.json`` before suggesting destructive edits.
# Stays small: the file is one of *many* rules a project may have, so
# we deliberately keep it focused on the presence-check contract and
# defer to ``AGENTS.md`` for everything else.
_CURSOR_RULES_DIRNAME: str = ".cursor/rules"
_CURSOR_RULE_FILENAME: str = "spec-team-presence.mdc"
_CURSOR_RULE_BODY: str = """\
---
description: Spec Live locks check before file edits
alwaysApply: true
---

# Spec Live — check coordination before editing

This bundle uses **Spec Live**. Before planning or editing, run `spec status`
to verify the machine workday switch and watcher, then read
`.spec/team-coordination.md` when it exists. It lists active agent
objectives, progress, claimed paths, and recent handoffs. Avoid
duplicating active work. The file is removed automatically when the
last active round finishes, so absence is normal.

Before you edit any file in this project, run:

```bash
spec locks check <bundle-relative-path>
```

The exit code is the contract:

- **0** → clear: fresh coordination data shows no overlapping claim or edit.
- **2** → at least one teammate likely has the file dirty. Show the
  warning and ask the user before overwriting.
- **3** → coordination health is unknown (missing/stale watcher data). Surface
  the degraded state instead of treating it as evidence of safety.

`spec locks check` ignores a **stale** `.spec/team-presence.json` (for
example when `spec watch` is not running) so you do not act on zombie
data. For a readable summary you can also open
`.spec/team-editing-brief.md` (updated by `spec watch`).

**Git push handoffs:** when teammates run `spec team request-push <handle>`
(or `/push@handle` from `spec team watch` in a bundle cwd), rows land in
`.spec/team-push-requests.yaml` and are merged into the same mirror files.
If **the user's Spec handle** matches `to_handle` in that YAML, help them
**commit** if needed and **`git push`** to `origin` on the listed branch
(never force-push unless the user explicitly asks).

Legacy: `spec presence check` is still available with its older 0/2 contract,
but it does not distinguish unknown or apply the unified task-claim evaluator.

You do **not** need to run this for files you are creating fresh
under your own scaffolding (no path conflict possible). Only run it
when modifying files that already exist or that other teammates
might also be touching.

This rule pairs with the Claude Code `PreToolUse` hook in
`.claude/settings.json`, which performs the same check
automatically for Edit / Write / MultiEdit tool calls.

The human operator controls local sharing with `spec on` at the start of the
workday and `spec off` when finished. Agents must not change that switch unless
the user explicitly asks. When it is OFF, say that cross-machine context is
unknown and do not describe the lock result as clear.
"""


_STARTER_AGENTS = (
    """# AGENTS.md — instructions for coding agents in this repo

This is a **Spec bundle**. The source of truth is plain English in
`docs/**/*.md` plus the captured conversational history in
`prompts/*.prompts`. Running code is a compile artifact.

## How to compile this bundle

When the user asks you to **compile**, **build**, or **generate** the
code for this bundle:

1. Run `spec compile` in the bundle root. That writes
   `.spec/compile-prompt.md`, a self-contained compile prompt
   derived from the current specs and every `.prompts` file in
   `prompts/`.
2. Read that file and follow the instructions inside it. In particular,
   emit generated files under `./out/` (or whatever `output.target` in
   `spec.yaml` says).
3. If the user edited a `.prompts` file, prefer its guidance over your
   own memory of past conversations — those files *are* the conversation.

## What files matter

- `docs/**/*.md` — **specs**. Plain English intent. Edit these to change
  what gets built.
- `prompts/*.prompts` — captured conversational history. One file per
  commit, each containing every session that produced that commit.
  Edit these to rewrite history (and therefore the next compile).
- `spec.yaml` — bundle manifest, model routing, output target.

## What NOT to do

- Don't put prompts in `.md` files. Prompts have their own extension
  (`.prompts`) and their own schema — `spec push` rejects `.md`
  files inside `prompts/`.
- Don't invent new top-level directories — the bundle structure is
  part of the contract with Spec Cloud.
- Don't edit files under `out/` by hand; they are regenerated on every
  compile.
- Don't commit `.spec/` — it's local index state.

"""
    + AGENTS_COORDINATION_BLOCK_BODY
    + """

## Git push handoffs (Spec Live)

Teammates can request that **you** push your branch so they can `git pull`:

- Read **`.spec/team-push-requests.yaml`** and the **Git push handoff** section
  in `.spec/team-editing-brief.md` (also copied into `.spec/team-presence.json`
  as `push_requests` when `spec watch` is running).
- If **your Spec handle** matches `to_handle`, help the user **commit** if
  needed and run **`git push`** to `origin` on the listed branch (never
  force-push unless the user explicitly asks).

## Team journal (optional)

`spec journal sync` materializes recent Spec Live prompt events from Cloud
under `docs/spec-journal/`. Run it only when the user asks to refresh that
history or when a live watcher is already connected and the journal is needed
for a handoff. It is not a pre-edit or pre-push gate: it requires network
access and may transfer project metadata to Spec Cloud.

For edit safety, use `spec locks check <bundle-relative-path>` as described
above. Missing or stale watcher data returns exit `3` (unknown), while an
overlapping claim returns exit `2`. Both states must be surfaced distinctly;
only exit `0` proves the current coordination view is clear.

A Claude Code `PreToolUse` hook is automatically wired into
`.claude/settings.json` by `spec init`, so Claude Code does this
check for you on every `Edit` / `Write` / `MultiEdit` /
`NotebookEdit`. Other agents (Cursor, Codex, generic LLMs reading
this file) should call `spec locks check` themselves.
"""
)


def _write_starter_manifest(path: Path, name: str, *, cloud_project: str) -> None:
    path.write_text(
        _STARTER_MANIFEST.format(name=name, cloud_project=cloud_project),
        encoding="utf-8",
    )


def _write_if_missing(path: Path, contents: str) -> bool:
    if path.exists():
        return False
    path.write_text(contents, encoding="utf-8")
    return True


def _install_agents_coordination_block(bundle_root: Path) -> tuple[str, Path]:
    """Create or refresh only Spec's managed coordination block in AGENTS.md."""
    path = bundle_root / AGENTS_FILENAME
    if not path.exists():
        path.write_text(AGENTS_COORDINATION_BLOCK_BODY, encoding="utf-8")
        return "installed", path

    existing = path.read_text(encoding="utf-8")
    has_begin = AGENTS_COORDINATION_BLOCK_BEGIN in existing
    has_end = AGENTS_COORDINATION_BLOCK_END in existing
    if has_begin != has_end:
        raise OSError(
            "AGENTS.md contains only one Spec Live coordination marker; "
            "restore or remove the incomplete managed block, then retry"
        )
    if has_begin:
        start = existing.index(AGENTS_COORDINATION_BLOCK_BEGIN)
        end = existing.index(AGENTS_COORDINATION_BLOCK_END) + len(
            AGENTS_COORDINATION_BLOCK_END
        )
        updated = (
            existing[:start]
            + AGENTS_COORDINATION_BLOCK_BODY.rstrip()
            + existing[end:]
        )
        if updated == existing:
            return "unchanged", path
        path.write_text(updated, encoding="utf-8")
        return "updated", path

    separator = "" if not existing or existing.endswith("\n") else "\n"
    path.write_text(
        existing + separator + "\n" + AGENTS_COORDINATION_BLOCK_BODY,
        encoding="utf-8",
    )
    return "appended", path


def refresh_agent_rules(bundle_root: Path) -> None:
    """Refresh the managed Cursor, Claude, and AGENTS coordination surfaces."""
    root = bundle_root.expanduser().resolve()
    cursor_rule_path = root / _CURSOR_RULES_DIRNAME / _CURSOR_RULE_FILENAME
    cursor_rule_path.parent.mkdir(parents=True, exist_ok=True)
    cursor_rule_path.write_text(_CURSOR_RULE_BODY, encoding="utf-8")
    from .hooks import install_claude_settings

    install_claude_settings(root, block_mode=False)
    _install_agents_coordination_block(root)
    remember_bundle(root)


def _render_starter_prompts(author_name: str, author_email: str) -> str:
    """Materialize the starter `.prompts` with real timestamps + ids so
    the file parses against `spec.prompts/v0.1` without hand-editing."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    # TOML offset-datetime literal; matches what `spec prompts capture`
    # writes in the rendered output (`_iso_z`).
    committed_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    # A UUID-shaped id. `os.urandom` is enough — we just need something
    # unique and stable once written.
    rnd = os.urandom(8).hex()
    session_id = f"{rnd[:8]}-{rnd[8:12]}-4{rnd[12:15]}-a{rnd[15:16]}00-000000000000"
    return _STARTER_PROMPTS_TEMPLATE.format(
        committed_at=committed_at,
        session_id=session_id,
        author_name=author_name,
        author_email=author_email,
    )


# ---------------------------------------------------------------------------
# Git hook installation
# ---------------------------------------------------------------------------


def _install_git_hook_segment(
    git_dir: Path,
    hook_filename: str,
    begin_marker: str,
    end_marker: str,
    hook_body: str,
    *,
    fresh_header: str,
) -> tuple[str, Path]:
    """Install or update one Spec block inside ``.git/hooks/<hook_filename>``.

    ``hook_body`` must include ``begin_marker`` … ``end_marker`` so re-init
    can replace in place. ``fresh_header`` is used only when creating a new
    hook file (use ``#!/bin/sh\\n\\n`` when ``set -e`` would be risky).
    """
    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    hook_path = hooks_dir / hook_filename

    if not hook_path.exists():
        hook_path.write_text(fresh_header + hook_body, encoding="utf-8")
        _chmod_executable(hook_path)
        return "installed", hook_path

    existing = hook_path.read_text(encoding="utf-8")
    if begin_marker in existing and end_marker in existing:
        start = existing.index(begin_marker)
        end = existing.index(end_marker) + len(end_marker)
        updated = existing[:start] + hook_body.rstrip() + existing[end:]
        if updated != existing:
            hook_path.write_text(updated, encoding="utf-8")
            _chmod_executable(hook_path)
            return "updated", hook_path
        _chmod_executable(hook_path)
        return "updated", hook_path

    separator = "" if existing.endswith("\n") else "\n"
    hook_path.write_text(existing + separator + "\n" + hook_body, encoding="utf-8")
    _chmod_executable(hook_path)
    return "appended", hook_path


def _git_hook_body_is_shell_stub_only(text: str) -> bool:
    """True if *text* is empty or only a minimal ``#!/bin/sh`` header (optional ``set -e``)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return True
    if lines == ["#!/bin/sh"]:
        return True
    if lines == ["#!/bin/sh", "set -e"]:
        return True
    return False


def _uninstall_git_hook_segment(
    git_dir: Path,
    hook_filename: str,
    begin_marker: str,
    end_marker: str,
) -> tuple[str, Path]:
    """Remove the Spec block from ``.git/hooks/<hook_filename>``.

    If the file is empty or only a shell stub after removal, delete the file.
    Returns ``(status, path)`` where *status* is ``missing``, ``no_spec_block``,
    ``removed`` (file deleted), or ``stripped`` (file kept with other content).
    """
    hook_path = git_dir / "hooks" / hook_filename
    if not hook_path.is_file():
        return "missing", hook_path
    try:
        existing = hook_path.read_text(encoding="utf-8")
    except OSError:
        return "no_spec_block", hook_path
    if begin_marker not in existing or end_marker not in existing:
        return "no_spec_block", hook_path
    start = existing.index(begin_marker)
    end = existing.index(end_marker) + len(end_marker)
    before = existing[:start].rstrip()
    after = existing[end:].lstrip()
    if before and after:
        updated = before + "\n\n" + after
    elif before:
        updated = before
    elif after:
        updated = after
    else:
        updated = ""
    core = updated.strip()
    if not core or _git_hook_body_is_shell_stub_only(core):
        try:
            hook_path.unlink()
        except OSError:
            return "no_spec_block", hook_path
        return "removed", hook_path
    out = updated if updated.endswith("\n") else updated + "\n"
    try:
        hook_path.write_text(out, encoding="utf-8")
    except OSError:
        return "no_spec_block", hook_path
    _chmod_executable(hook_path)
    return "stripped", hook_path


def _chmod_executable(path: Path) -> None:
    # ``chmod +x`` without clobbering existing bits. No-op on Windows
    # (permissions aren't POSIX there; git for Windows ignores the bit
    # and runs hooks via its shim regardless).
    try:
        current = path.stat().st_mode
        path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def _install_gitignore_block(repo_root: Path) -> tuple[str, Path]:
    """Install or update the Spec-managed ``.gitignore`` block.

    Returns ``(status, path)`` where ``status`` is one of:
        "installed"  — fresh ``.gitignore`` written from scratch
        "appended"   — block added to an existing user-authored file
        "updated"    — replaced an existing Spec block in place
        "unchanged"  — block already matches; no write performed
    """
    path = repo_root / ".gitignore"

    if not path.exists():
        path.write_text(GITIGNORE_BLOCK_BODY, encoding="utf-8")
        return "installed", path

    existing = path.read_text(encoding="utf-8")
    if GITIGNORE_BLOCK_BEGIN in existing and GITIGNORE_BLOCK_END in existing:
        start = existing.index(GITIGNORE_BLOCK_BEGIN)
        end = existing.index(GITIGNORE_BLOCK_END) + len(GITIGNORE_BLOCK_END)
        updated = existing[:start] + GITIGNORE_BLOCK_BODY.rstrip() + existing[end:]
        if updated == existing:
            return "unchanged", path
        path.write_text(updated, encoding="utf-8")
        return "updated", path

    separator = "" if existing.endswith("\n") else "\n"
    path.write_text(
        existing + separator + "\n" + GITIGNORE_BLOCK_BODY,
        encoding="utf-8",
    )
    return "appended", path


def _stage_scaffold_for_push(
    root: Path,
    *,
    wrote_prompts: bool,
    starter_prompts_name: str,
    agents_written: bool,
) -> list[str]:
    """Hash-and-record scaffolded spec files so `spec push` does not
    require a redundant `spec add spec.yaml` after a fresh `spec init`.

    Also stamps the bundle's current absolute path into
    ``index.bundle_paths`` so a future ``spec prompts capture`` can
    still find sessions captured under this path even if the folder
    has been renamed in the meantime — see ``stage.record_bundle_path``.
    """
    from ..stage import load_index, save_index, sha256

    rels: list[str] = [MANIFEST_FILENAME, "docs/product.md"]
    if wrote_prompts:
        rels.append(f"{PROMPTS_DIRNAME}/{starter_prompts_name}")
    if agents_written:
        rels.append(AGENTS_FILENAME)
    idx = load_index(root)
    staged: list[str] = []
    for rel in rels:
        p = root / rel
        if p.is_file():
            idx.staged[rel] = sha256(p.read_bytes())
            staged.append(rel)
    resolved = str(root.resolve())
    if resolved not in idx.bundle_paths:
        idx.bundle_paths.append(resolved)
    if staged or resolved in idx.bundle_paths:
        save_index(idx)
    return staged


@click.command("init")
@click.option(
    "--name",
    "-n",
    default=None,
    help="Bundle name. Defaults to the git origin's repo name "
    "(matching GitHub mental model), falling back to the directory "
    "name when there is no `origin` remote.",
)
@click.option("--force", is_flag=True, help="Overwrite an existing spec.yaml.")
@click.option(
    "--skip-git-hook",
    is_flag=True,
    help="Don't install Spec git hooks (pre-commit, commit-msg, post-commit stub, pre-push).",
)
@click.option(
    "--skip-gitignore",
    is_flag=True,
    help="Don't add the Spec-managed `.gitignore` block at the repo root.",
)
@click.option(
    "--upgrade-rules",
    is_flag=True,
    help=(
        "Refresh Spec Live coordination files only: overwrite "
        "`.cursor/rules/spec-team-presence.mdc` and re-apply the Spec-managed "
        "Claude Code hook and `AGENTS.md` blocks. "
        "Does not touch spec.yaml or docs — for CLI upgrades in existing bundles."
    ),
)
def init_cmd(
    name: str | None,
    force: bool,
    skip_git_hook: bool,
    skip_gitignore: bool,
    upgrade_rules: bool,
) -> None:
    """Scaffold a starter bundle in the current directory."""
    if upgrade_rules:
        try:
            bundle_root = find_bundle_root()
        except BundleNotFoundError as e:
            fatal(str(e))
            return
        try:
            refresh_agent_rules(bundle_root)
        except OSError as e:
            fatal(f"Could not refresh agent rules: {e}")
            return
        ok(
            "Spec Live rules refreshed — `.cursor/rules/spec-team-presence.mdc` "
            "plus Spec-managed `.claude/settings.json` and `AGENTS.md` blocks."
        )
        dim(f"bundle root: {bundle_root}")
        return

    cwd = Path.cwd().resolve()

    # Local imports keep the cold-start path light when init isn't the
    # invoked command (Click still imports the module on every run).
    from ..git import read_git_context, read_origin_url, repo_toplevel

    git_root = repo_toplevel(cwd)
    if git_root is None:
        fatal(
            "Spec can only initialize a Git repository. Run `git init` "
            "first, then run `spec init` again."
        )
        return
    root = git_root.resolve()
    if cwd != root:
        dim(f"Using Git repository root: {root}")

    manifest_path = root / MANIFEST_FILENAME
    if manifest_path.exists() and not force:
        fatal(f"{MANIFEST_FILENAME} already exists. Re-run with --force to overwrite.")

    git_ctx = read_git_context(root)

    # Bundle-name precedence:
    #   --name flag > git origin remote (if it parses) > current directory
    # The git path matches GitHub's mental model: a repo cloned as
    # `acme/billing-service` becomes a bundle named `billing-service`.
    # We surface where the name came from in the output so users aren't
    # surprised by a name they didn't type.
    name_origin: str
    name_origin_detail: str | None = None
    if name is not None:
        bundle_name = name
        name_origin = "flag"
    else:
        inferred: str | None = None
        origin_url: str | None = None
        if git_ctx.is_repo:
            origin_url = read_origin_url(root)
            inferred = repo_name_from_remote_url(origin_url)
        if inferred:
            bundle_name = inferred
            name_origin = "git"
            name_origin_detail = origin_url
        else:
            bundle_name = root.name
            name_origin = "dir"

    from ..config import load_credentials

    creds = load_credentials()
    handle = ""
    if creds and creds.access_token and isinstance(creds.user_handle, str):
        handle = creds.user_handle.strip().lower()
    cloud_project_value = f"{handle}/{bundle_name}" if handle else bundle_name
    _write_starter_manifest(manifest_path, bundle_name, cloud_project=cloud_project_value)

    docs_dir = root / "docs"
    docs_dir.mkdir(exist_ok=True)
    _write_if_missing(docs_dir / "product.md", _STARTER_DOC)

    # `prompts/` with a starter `.prompts` file. Giving users a concrete,
    # valid-against-v0.1 example on day one is faster than pointing them
    # at the format doc — they edit the file in place. Skip if one already
    # exists; `spec prompts capture` will extend from there.
    prompts_dir = root / PROMPTS_DIRNAME
    prompts_dir.mkdir(exist_ok=True)

    author_name = git_ctx.author_name or "You"
    author_email = git_ctx.author_email or "you@example.com"

    starter_prompts_name = "0000-starter.prompts"
    starter_prompts_path = prompts_dir / starter_prompts_name
    wrote_prompts = False
    if not any(prompts_dir.glob("*.prompts")):
        starter_prompts_path.write_text(
            _render_starter_prompts(author_name, author_email),
            encoding="utf-8",
        )
        wrote_prompts = True

    # AGENTS.md — so Claude Code / other agents know what to do when
    # asked to compile. Written only if missing to respect pre-existing
    # project conventions.
    agents_written = _write_if_missing(root / AGENTS_FILENAME, _STARTER_AGENTS)
    agents_coordination_status: str | None = None
    if not agents_written:
        try:
            agents_coordination_status, _ = _install_agents_coordination_block(root)
        except OSError as e:
            info("")
            dim(f"Could not update {AGENTS_FILENAME} ({e}). Skipping.")

    # Spec Live integrations for AI IDEs:
    #   * `.cursor/rules/spec-team-presence.mdc` — universal "always
    #     apply" rule that tells Cursor + any LLM reading the rules
    #     dir to call `spec presence check` before edits.
    #   * `.claude/settings.json` — PreToolUse hook that does the
    #     same check automatically for Claude Code's edit tools.
    # Both are idempotent and respect pre-existing user files (the
    # Cursor rule only writes when missing; the Claude settings
    # surgically replace just the Spec-managed entry).
    cursor_rule_written = False
    cursor_rule_path = root / _CURSOR_RULES_DIRNAME / _CURSOR_RULE_FILENAME
    try:
        cursor_rule_path.parent.mkdir(parents=True, exist_ok=True)
        cursor_rule_written = _write_if_missing(
            cursor_rule_path, _CURSOR_RULE_BODY
        )
    except OSError as e:
        info("")
        dim(f"Could not write Cursor rule ({e}). Skipping.")

    claude_settings_written = False
    claude_settings_path: Path | None = None
    try:
        from .hooks import install_claude_settings

        claude_settings_path = install_claude_settings(root, block_mode=False)
        claude_settings_written = True
    except OSError as e:
        info("")
        dim(f"Could not write .claude/settings.json ({e}). Skipping.")

    # Git hooks: pre-commit mirrors git↔spec staging, commit-msg captures
    # prompts, pre-push runs `spec push`. Skipped outside a git worktree or
    # with --skip-git-hook.
    hook_reports: list[tuple[str, Path, str]] = []
    git_dir = find_git_dir(root)
    if not skip_git_hook and git_dir is not None:
        try:
            for label, fname, beg, end, body, hdr in GIT_HOOK_INSTALL_ROWS:
                st, pth = _install_git_hook_segment(
                    git_dir, fname, beg, end, body, fresh_header=hdr
                )
                hook_reports.append((label, pth, st))
        except OSError as e:
            # A read-only hooks dir shouldn't fail the whole init.
            info("")
            dim(f"Could not install git hooks ({e}). Skipping.")

    # Top-level `.gitignore` block. Lives at the worktree root (not the
    # bundle root, when they differ) so engineers see Spec's ignored
    # paths in the same file as their own. ``.spec/`` already
    # self-ignores via an inner ``.gitignore``; the duplicate entry here
    # is for review hygiene — git takes the union.
    gitignore_status: str | None = None
    gitignore_path: Path | None = None
    if not skip_gitignore:
        worktree_root = repo_toplevel(root) if git_ctx.is_repo else None
        if worktree_root is not None:
            try:
                gitignore_status, gitignore_path = _install_gitignore_block(
                    worktree_root,
                )
            except OSError as e:
                # A read-only worktree shouldn't fail the whole init.
                info("")
                dim(f"Could not update .gitignore ({e}). Skipping.")

    auto_staged = _stage_scaffold_for_push(
        root,
        wrote_prompts=wrote_prompts,
        starter_prompts_name=starter_prompts_name,
        agents_written=(
            agents_written or agents_coordination_status in {"appended", "updated"}
        ),
    )
    remember_bundle(root)

    ok(f"Initialized bundle [bold]{bundle_name}[/] in {root}")
    if name_origin == "git" and name_origin_detail:
        dim(f"  name inferred from git remote: {name_origin_detail}")
    elif name_origin == "dir":
        dim(f"  name inferred from directory: {root.name}")
    pointer("manifest    ", str(manifest_path.relative_to(root)))
    pointer("entry       ", "docs/product.md")
    pointer("prompts     ", f"{PROMPTS_DIRNAME}/")
    if wrote_prompts:
        pointer("  starter   ", f"{PROMPTS_DIRNAME}/{starter_prompts_name}")
    if agents_written:
        pointer("agents      ", "AGENTS.md")
    elif agents_coordination_status in {"appended", "updated"}:
        pointer(
            "agents      ",
            f"AGENTS.md ({agents_coordination_status} Spec coordination block)",
        )
    else:
        dim("AGENTS.md already contains current Spec coordination instructions.")
    if cursor_rule_written:
        try:
            rel = cursor_rule_path.relative_to(root)
        except ValueError:
            rel = cursor_rule_path
        pointer("cursor rule ", str(rel))
    if claude_settings_written and claude_settings_path is not None:
        try:
            rel = claude_settings_path.relative_to(root)
        except ValueError:
            rel = claude_settings_path
        pointer("claude hook ", f"{rel} (PreToolUse → spec presence)")

    if auto_staged:
        dim(
            "Staged for `spec push`: " + ", ".join(auto_staged) + "."
        )

    if hook_reports:
        # The labels here have to match what the hooks ACTUALLY do today
        # (cf. `commands/git_hooks.py` and `commands/prompts.py`):
        #   * pre-commit  — captures Cursor/Claude/Codex turns into
        #                   `prompts/<branch>.prompts` AND mirrors
        #                   `git add` / `git rm` into `spec add` /
        #                   `spec unstage` for bundle-eligible files.
        #   * commit-msg  — installed but currently a no-op stub
        #                   (capture moved into pre-commit).
        #   * post-commit — deprecated stub; harmless.
        #   * pre-push    — runs `spec push` on branch-ref pushes
        #                   (gated on `SKIP_SPEC_PUSH` and tag-only).
        #   * post-merge  — rolls captured prompts forward across
        #                   merges so trunk's `.prompts` file stays
        #                   the canonical narrative.
        # Don't lie to the user about commit-msg here; the previous
        # message claimed it ran ``spec git-hooks commit-msg`` which
        # was true historically but is now a no-op.
        dim(
            "Git hooks installed: "
            "pre-commit (capture + mirror staging) · "
            "pre-push (`spec push`; opt out: SKIP_SPEC_PUSH=1 or git push --no-verify) · "
            "post-merge (prompts rollup)."
        )
        for label, hook_path, st in hook_reports:
            try:
                rel = (
                    hook_path.relative_to(root)
                    if hook_path.is_relative_to(root)
                    else hook_path
                )
            except ValueError:
                rel = hook_path
            pointer(f"git hook ({label})", f"{rel} ({st})")
    elif skip_git_hook:
        dim("Skipped git hook installation (--skip-git-hook).")
    elif git_dir is None:
        dim(
            "Not a git worktree — skipped git hooks. Run `git init` "
            "(the spec installer wires `git init` to also run `spec init`), "
            "or rerun `spec init --force` after the worktree exists."
        )

    if gitignore_status and gitignore_path is not None:
        try:
            rel_gi = gitignore_path.relative_to(root)
        except ValueError:
            rel_gi = gitignore_path
        pointer("gitignore   ", f"{rel_gi} ({gitignore_status})")
        if gitignore_status in ("installed", "appended", "updated"):
            dim("  ignores `.spec/` and `out/` (compile artifacts)")
    elif skip_gitignore:
        dim("Skipped .gitignore update (--skip-gitignore).")

    # Friendly pointer: is Claude Code actually installed on this box? We
    # don't fail the command if it isn't; `--via api` is still a valid
    # path. But the workflow is materially nicer with Claude Code.
    store = claude_code_store_root()
    if not store.exists():
        info("")
        dim("Claude Code not detected. The default compile flow expects it:")
        dim("  https://claude.ai/code")
        dim("Or compile via API: `spec compile --via api`.")

    info("")
    dim("Next: edit docs/product.md, then `spec add .` and `spec push`.")
    if wrote_prompts:
        dim(
            f"Edit {PROMPTS_DIRNAME}/{starter_prompts_name} to describe why this "
            "bundle exists — reviewers read it before the spec."
        )
    dim("When ready: `spec compile` and tell Claude Code to compile.")
