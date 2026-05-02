"""`spec init` — scaffold a new bundle in the current directory."""

from __future__ import annotations

import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

import click

from ..constants import MANIFEST_FILENAME, PROMPTS_DIRNAME
from ..git import find_git_dir
from ..sources.claude_code import claude_code_store_root
from ..ui import dim, fatal, info, ok, pointer


AGENTS_FILENAME: str = "AGENTS.md"


# Strip the trailing ``.git`` and pull the last path segment out of any
# remote URL git itself accepts on the CLI:
#   - ssh-shorthand: ``git@github.com:owner/repo.git`` (note the ``:``)
#   - https:         ``https://github.com/owner/repo.git``
#   - ssh-with-scheme: ``ssh://git@github.com/owner/repo.git``
#   - file://, gitlab, bitbucket, custom hosts — anything whose path ends
#     in ``…/<repo>`` or ``…/<repo>.git`` works; we don't validate hosts.
# A best-effort match: pathological inputs (empty, ``.``, ``.git``) fall
# through to ``None`` so the caller can default to ``Path.cwd().name``.
_REPO_NAME_RE = re.compile(
    r"""
    (?:[:/])               # `:` for ssh-shorthand, `/` otherwise
    (?P<repo>[^/:\s]+?)    # the last path segment (lazy, no separators)
    (?:\.git)?             # optional .git suffix
    /*\s*$                 # trailing slashes / whitespace, end of string
    """,
    re.VERBOSE,
)


def _repo_name_from_remote(url: str | None) -> str | None:
    """Infer a sensible bundle name from a git remote URL.

    Returns ``None`` for empty / unparseable / pathological inputs so
    callers can fall back to ``Path.cwd().name`` cleanly. The extracted
    name is *not* slugified — bundle names are free-form text in
    ``spec.yaml`` and the cloud only cares about the slug, which is
    derived server-side.
    """
    if not url or not isinstance(url, str):
        return None
    match = _REPO_NAME_RE.search(url.strip())
    if not match:
        return None
    name = match.group("repo").strip()
    if not name or name in (".", "..", ".git"):
        return None
    return name


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
{GITIGNORE_BLOCK_END}
"""

# Git hooks installed under `.git/hooks/`. Each block is non-destructive:
# re-running `spec init` replaces only the Spec-managed segment between
# the sentinels. `--skip-git-hook` skips all three.

PRE_COMMIT_HOOK_BEGIN: str = "# >>> spec pre-commit >>>"
PRE_COMMIT_HOOK_END: str = "# <<< spec pre-commit <<<"
PRE_COMMIT_HOOK_BODY: str = f"""\
{PRE_COMMIT_HOOK_BEGIN}
# Auto-installed by `spec init`. Runs before the commit is recorded:
# mirrors paths you `git add`-ed into `spec add` (and removals into
# `spec unstage`) so spec staging tracks the same bundle files as git.
# Never blocks the commit — failures are swallowed per line below.
if command -v spec >/dev/null 2>&1; then
  spec git-hooks pre-commit || true
else
  echo "spec: CLI not on PATH; skipping spec/git index sync." >&2
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
# Auto-installed by `spec init`. Runs before git records the commit: captures
# new sessions into prompts/<branch>.prompts and git-adds them so they ship in
# the same commit. Failures are swallowed — capture never blocks a commit.
if command -v spec >/dev/null 2>&1; then
  spec git-hooks commit-msg "$1" || true
else
  echo "spec: CLI not on PATH; skipping prompts capture." >&2
fi
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
  project: {name}
  # `bundle_id:` is stamped here automatically on the first successful
  # `spec push` (PLAN.md §11). Once set, every push verifies it against
  # the remote — pointing `cloud.project` at an unrelated bundle by
  # accident is then a hard refusal, not a silent overwrite.
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
def init_cmd(
    name: str | None,
    force: bool,
    skip_git_hook: bool,
    skip_gitignore: bool,
) -> None:
    """Scaffold a starter bundle in the current directory."""
    root = Path.cwd().resolve()

    manifest_path = root / MANIFEST_FILENAME
    if manifest_path.exists() and not force:
        fatal(f"{MANIFEST_FILENAME} already exists. Re-run with --force to overwrite.")

    # Local imports keep the cold-start path light when init isn't the
    # invoked command (Click still imports the module on every run).
    from ..git import read_git_context, read_origin_url, repo_toplevel

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
            inferred = _repo_name_from_remote(origin_url)
        if inferred:
            bundle_name = inferred
            name_origin = "git"
            name_origin_detail = origin_url
        else:
            bundle_name = root.name
            name_origin = "dir"

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
        agents_written=agents_written,
    )

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
    else:
        dim("AGENTS.md already exists — left untouched.")

    if auto_staged:
        dim(
            "Staged for `spec push`: " + ", ".join(auto_staged) + "."
        )

    if hook_reports:
        dim(
            "Git hooks: pre-commit → `spec git-hooks pre-commit` · "
            "commit-msg → `spec git-hooks commit-msg` · "
            "pre-push → `spec git-hooks pre-push` "
            "(skip push: SKIP_SPEC_PUSH=1 or git push --no-verify)"
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
            "Not a git worktree — skipped git hooks. Run "
            "`git init && spec init --force` to install them, or wire "
            "`spec prompts capture` / `spec push` manually."
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
