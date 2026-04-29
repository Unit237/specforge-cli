"""`spec init` — scaffold a new bundle in the current directory."""

from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import click

from ..constants import MANIFEST_FILENAME, PROMPTS_DIRNAME
from ..sources.claude_code import claude_code_store_root
from ..ui import dim, fatal, info, ok, pointer


AGENTS_FILENAME: str = "AGENTS.md"

# The `.git/hooks/post-commit` we install. Contract:
#   - Runs `spec prompts capture` after every commit.
#   - Non-fatal: if the CLI is missing or capture errors, we print a hint
#     and exit 0 so a capture failure never blocks a commit.
#   - Idempotent: the Spec-managed section is delimited by these
#     sentinels so re-running `spec init` only replaces our block
#     and leaves anything else alone.
POST_COMMIT_HOOK_BEGIN: str = "# >>> spec post-commit >>>"
POST_COMMIT_HOOK_END: str = "# <<< spec post-commit <<<"
POST_COMMIT_HOOK_BODY: str = f"""\
{POST_COMMIT_HOOK_BEGIN}
# Auto-installed by `spec init`. Captures new conversational
# sessions into prompts/<timestamp>.prompts after every commit, so a
# single `git commit` produces both the code delta and the trail of
# prompts that produced it.
#
# Safe to delete; re-run `spec init` to reinstall.
if command -v spec >/dev/null 2>&1; then
  # --source all is the default; --since filter is off so we pick up
  # anything captured between commits. Non-zero is swallowed: we never
  # want a capture failure to block a commit.
  spec prompts capture || true
else
  echo "spec: CLI not on PATH; skipping prompts capture." >&2
fi
{POST_COMMIT_HOOK_END}
"""


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
# `spec prompts capture` (run automatically by the post-commit git
# hook) appends new captured sessions here; you can also hand-edit this
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
  project: {name}
"""


_STARTER_AGENTS = """# AGENTS.md — instructions for coding agents in this repo

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


def _write_starter_manifest(path: Path, name: str) -> None:
    path.write_text(
        _STARTER_MANIFEST.format(name=name),
        encoding="utf-8",
    )


def _write_if_missing(path: Path, contents: str) -> bool:
    if path.exists():
        return False
    path.write_text(contents, encoding="utf-8")
    return True


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


def _find_git_dir(root: Path) -> Path | None:
    """Resolve the ``.git`` directory for ``root``.

    Handles the two common shapes:
      - plain checkout: ``<root>/.git`` is a directory
      - worktree / submodule: ``<root>/.git`` is a text file of the form
        ``gitdir: /absolute/path/to/.git/worktrees/<name>``

    Returns ``None`` when ``root`` isn't inside a git repo at all — init
    still works, it just skips hook installation with a hint.
    """
    candidate = root / ".git"
    if candidate.is_dir():
        return candidate
    if candidate.is_file():
        try:
            first = candidate.read_text(encoding="utf-8").strip().splitlines()[0]
        except (OSError, UnicodeDecodeError):
            return None
        if first.startswith("gitdir:"):
            target = Path(first.split(":", 1)[1].strip())
            if not target.is_absolute():
                target = (root / target).resolve()
            if target.is_dir():
                return target
    # Walk upward in case init is run from a nested directory. `git rev-parse
    # --show-toplevel` would be more correct, but spawning git just for this
    # is heavyweight and this is already a best-effort convenience.
    parent = root.parent
    if parent != root:
        return _find_git_dir(parent)
    return None


def _install_post_commit_hook(git_dir: Path) -> tuple[str, Path]:
    """Install or update the Spec post-commit hook.

    Returns ``(status, path)`` where ``status`` is one of:
        "installed"  — fresh hook
        "updated"    — replaced an existing Spec block in place
        "appended"   — added our block to an existing user-authored hook
    """
    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    hook_path = hooks_dir / "post-commit"

    if not hook_path.exists():
        hook_path.write_text("#!/bin/sh\nset -e\n\n" + POST_COMMIT_HOOK_BODY, encoding="utf-8")
        _chmod_executable(hook_path)
        return "installed", hook_path

    existing = hook_path.read_text(encoding="utf-8")
    if POST_COMMIT_HOOK_BEGIN in existing and POST_COMMIT_HOOK_END in existing:
        start = existing.index(POST_COMMIT_HOOK_BEGIN)
        end = existing.index(POST_COMMIT_HOOK_END) + len(POST_COMMIT_HOOK_END)
        updated = existing[:start] + POST_COMMIT_HOOK_BODY.rstrip() + existing[end:]
        if updated != existing:
            hook_path.write_text(updated, encoding="utf-8")
            _chmod_executable(hook_path)
            return "updated", hook_path
        _chmod_executable(hook_path)
        return "updated", hook_path

    separator = "" if existing.endswith("\n") else "\n"
    hook_path.write_text(existing + separator + "\n" + POST_COMMIT_HOOK_BODY, encoding="utf-8")
    _chmod_executable(hook_path)
    return "appended", hook_path


def _chmod_executable(path: Path) -> None:
    # ``chmod +x`` without clobbering existing bits. No-op on Windows
    # (permissions aren't POSIX there; git for Windows ignores the bit
    # and runs hooks via its shim regardless).
    try:
        current = path.stat().st_mode
        path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def _stage_scaffold_for_push(
    root: Path,
    *,
    wrote_prompts: bool,
    starter_prompts_name: str,
    agents_written: bool,
) -> list[str]:
    """Hash-and-record scaffolded spec files so `spec push` does not
    require a redundant `spec add spec.yaml` after a fresh `spec init`."""
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
    if staged:
        save_index(idx)
    return staged


@click.command("init")
@click.option("--name", "-n", default=None, help="Bundle name (defaults to directory name).")
@click.option("--force", is_flag=True, help="Overwrite an existing spec.yaml.")
@click.option(
    "--skip-git-hook",
    is_flag=True,
    help="Don't install the post-commit hook even if this is a git repo.",
)
def init_cmd(name: str | None, force: bool, skip_git_hook: bool) -> None:
    """Scaffold a starter bundle in the current directory."""
    root = Path.cwd().resolve()
    bundle_name = name or root.name

    manifest_path = root / MANIFEST_FILENAME
    if manifest_path.exists() and not force:
        fatal(f"{MANIFEST_FILENAME} already exists. Re-run with --force to overwrite.")

    _write_starter_manifest(manifest_path, bundle_name)

    docs_dir = root / "docs"
    docs_dir.mkdir(exist_ok=True)
    _write_if_missing(docs_dir / "product.md", _STARTER_DOC)

    # `prompts/` with a starter `.prompts` file. Giving users a concrete,
    # valid-against-v0.1 example on day one is faster than pointing them
    # at the format doc — they edit the file in place. Skip if one already
    # exists; `spec prompts capture` will extend from there.
    prompts_dir = root / PROMPTS_DIRNAME
    prompts_dir.mkdir(exist_ok=True)
    from ..git import read_git_context  # local import: avoids a hard dep
                                        # on the git helper at module load.

    git_ctx = read_git_context(root)
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

    # Git post-commit hook. Run capture automatically so a `git commit`
    # produces both the code delta and the prompts that made it. Skipped
    # when we're not inside a git worktree, or when --skip-git-hook is set.
    hook_status: str | None = None
    hook_path: Path | None = None
    git_dir = _find_git_dir(root)
    if not skip_git_hook and git_dir is not None:
        try:
            hook_status, hook_path = _install_post_commit_hook(git_dir)
        except OSError as e:
            # A read-only hooks dir shouldn't fail the whole init.
            info("")
            dim(f"Could not install post-commit hook ({e}). Skipping.")

    auto_staged = _stage_scaffold_for_push(
        root,
        wrote_prompts=wrote_prompts,
        starter_prompts_name=starter_prompts_name,
        agents_written=agents_written,
    )

    ok(f"Initialized bundle [bold]{bundle_name}[/] in {root}")
    pointer("manifest    ", str(manifest_path.relative_to(root)))
    pointer("entry       ", "docs/product.md")
    pointer("prompts     ", f"{PROMPTS_DIRNAME}/")
    if wrote_prompts:
        pointer("  starter   ", f"{PROMPTS_DIRNAME}/{starter_prompts_name}")
    if agents_written:
        pointer("agents      ", "AGENTS.md")
    else:
        dim("AGENTS.md already exists — left untouched.")

    if auto_staged:
        dim(
            "Staged for `spec push`: " + ", ".join(auto_staged) + "."
        )

    if hook_status and hook_path is not None:
        rel = hook_path.relative_to(root) if hook_path.is_relative_to(root) else hook_path
        pointer("git hook    ", f".git/hooks/post-commit ({hook_status})")
        dim(f"  runs `spec prompts capture` after every git commit ({rel})")
    elif skip_git_hook:
        dim("Skipped git hook installation (--skip-git-hook).")
    elif git_dir is None:
        dim(
            "Not a git worktree — skipped post-commit hook. Run "
            "`git init && spec init --force` to install it, or wire "
            "`spec prompts capture` into your workflow manually."
        )

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
