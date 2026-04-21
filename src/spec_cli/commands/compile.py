"""`spec compile` — produce a compile prompt for Claude Code (default),
or shell out to `spec-compile` for API-driven compilation.

Design:

Default mode (Claude-Code-first). Assembles a deterministic "compile prompt"
from the bundle's specs, prompt templates, and `.prompt` sessions, and
writes it to `.spec/compile-prompt.md`. The project's `AGENTS.md`
tells Claude Code what to do with that file on the next "compile" request.
No API keys, no LLM SDKs, no network — the user's existing agent does the
inference.

  $ spec compile
  ✓ compile prompt ready · .spec/compile-prompt.md
  Open Claude Code here and say "compile".

API mode. Shells out to `spec-compile` (the sibling compiler package)
which loads SDKs, calls a model directly, and writes files to `./out`.

  $ spec compile --via api [--dry-run --model … --out …]

Stdout mode. Prints the assembled compile prompt to stdout. Useful for
piping into other agents, or for `diff`-ing two compile prompts in code
review.

  $ spec compile --stdout | pbcopy
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import click

from ..compile_assembly import assemble_bundle, render_compile_prompt
from ..config import BundleNotFoundError, find_bundle_root, load_manifest
from ..stage import INDEX_DIRNAME
from ..ui import console, dim, fatal, info, ok, pointer


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

COMPILE_PROMPT_FILENAME: str = "compile-prompt.md"


def _compile_prompt_path(root: Path) -> Path:
    return root / INDEX_DIRNAME / COMPILE_PROMPT_FILENAME


# ---------------------------------------------------------------------------
# API mode — shell out to the separate compiler package
# ---------------------------------------------------------------------------


def _locate_compiler() -> str | None:
    """Find `spec-compile` on PATH, or next to the running `spec`
    binary (same venv / same pipx-managed dir)."""
    found = shutil.which("spec-compile")
    if found:
        return found
    candidate = Path(sys.executable).parent / "spec-compile"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _run_api_mode(root: Path, extra_args: list[str]) -> int:
    exe = _locate_compiler()
    if not exe:
        dim("--via api requires the Spec compiler package (a separate install).")
        info("Install it, then re-run:")
        info("  pip install spec-compiler")
        info("")
        info("Or use the default Claude-Code flow:")
        info("  spec compile")
        return 127

    cmd = [exe, str(root), *extra_args]
    try:
        return subprocess.call(cmd, env=os.environ.copy())
    except KeyboardInterrupt:
        return 130
    except (FileNotFoundError, PermissionError) as e:
        # `exe` resolved on PATH but can't actually be executed — typically
        # a broken symlink from an old install. Surface it cleanly rather
        # than spewing a Python traceback.
        dim(f"Could not launch {exe}: {e}")
        info("Try reinstalling the compiler:  pip install --force-reinstall spec-compiler")
        return 127


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


@click.command(
    "compile",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option(
    "--via",
    type=click.Choice(["claude-code", "api"], case_sensitive=False),
    default="claude-code",
    help=(
        "How to run compilation. `claude-code` (default) writes a self-contained "
        "compile prompt you hand to your running Claude Code session. `api` "
        "shells out to `spec-compile` and calls the model directly."
    ),
)
@click.option(
    "--stdout",
    "to_stdout",
    is_flag=True,
    help="Print the assembled compile prompt to stdout instead of writing a file.",
)
@click.pass_context
def compile_cmd(ctx: click.Context, via: str, to_stdout: bool) -> None:
    """
    Compile this bundle.

    Default (Claude Code): writes `.spec/compile-prompt.md`, which
    your running Claude Code session will read on the next "compile"
    request (see AGENTS.md at the bundle root).

    API mode: shells out to `spec-compile` with any extra flags
    forwarded verbatim, e.g.

      spec compile --via api --dry-run --model claude-sonnet-4-5
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    mode = via.lower()

    if mode == "api":
        if to_stdout:
            fatal("--stdout cannot be combined with --via api.")
            return
        rc = _run_api_mode(root, list(ctx.args))
        sys.exit(rc)

    # Claude-Code-first path. --stdout and the default write-to-file share
    # the same assembler; only output destination differs.
    if ctx.args:
        # Extra positional args are reserved for `--via api` mode today.
        fatal(
            f"Unexpected argument(s) {ctx.args!r}. "
            f"Did you mean `spec compile --via api {' '.join(ctx.args)}`?"
        )
        return

    try:
        manifest = load_manifest(root)
    except Exception as e:  # noqa: BLE001
        fatal(f"Could not read spec.yaml: {e}")
        return

    bundle = assemble_bundle(root, manifest)
    if not bundle.spec_files:
        fatal(
            "No spec files matched `spec.entry` / `spec.include` in spec.yaml. "
            "Add at least one .md under docs/ and try again."
        )
        return

    prompt_text = render_compile_prompt(bundle)

    if to_stdout:
        sys.stdout.write(prompt_text)
        return

    dest = _compile_prompt_path(root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Ensure .spec/.gitignore covers the compile prompt too — it's
    # generated output, not source.
    gi = dest.parent / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")
    dest.write_text(prompt_text, encoding="utf-8")

    console.print(
        f"[sf.label]compile[/] [sf.muted]· "
        f"{len(bundle.spec_files)} spec file(s), "
        f"{len(bundle.prompt_templates)} prompt template(s), "
        f"{len(bundle.session_files)} session(s)[/]"
    )
    ok(f"compile prompt ready · {dest.relative_to(root)}")
    pointer("next", "open Claude Code in this directory and say \"compile\"")
    dim(
        "Or: `spec compile --via api` to call an Anthropic model directly "
        "(requires `spec-compiler` + ANTHROPIC_API_KEY)."
    )
