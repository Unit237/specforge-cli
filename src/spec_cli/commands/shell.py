"""`spec shell` — manage the `git init` → `spec init` shell wrapper.

The curl installer (``install.sh``) runs ``spec shell install`` for you,
so most users never type these commands directly. They exist for users
on the manual-install path, for switching shells, for auditing the
wrapper text, and for clean uninstallation.

Git itself has no post-init hook: the only way to make ``spec init`` run
"alongside" ``git init`` is to wrap the ``git`` command in the user's
interactive shell. ``spec shell install`` writes a tiny POSIX shell
function into the user's rc file between sentinel markers (same pattern
as the git-hooks installer) so the wrapper is idempotent and removable.

The wrapper is deliberately conservative:

* Only acts when the first argument is ``init``.
* Skips ``--bare`` / ``--shared=*`` (those aren't bundle targets).
* Skips when ``spec.yaml`` already exists at the target dir.
* Preserves git's exit code regardless of what the spec init step does.
* Falls back to the unwrapped ``git`` if the ``spec`` binary is gone.
"""

from __future__ import annotations

import os
from pathlib import Path

import click

from ..ui import dim, fatal, info, ok, pointer


SHELL_INTEGRATION_BEGIN: str = "# >>> spec shell integration >>>"
SHELL_INTEGRATION_END: str = "# <<< spec shell integration <<<"


# POSIX-ish wrapper. Works in bash and zsh (both have ``local``). We
# scope every helper variable behind the ``__spec_`` prefix so we don't
# clobber anything the user may have. The ``return $__spec_rc`` line
# preserves git's own exit code so callers like ``git init &&
# something`` keep behaving as before.
SHELL_INTEGRATION_BODY_BASH_ZSH: str = f"""\
{SHELL_INTEGRATION_BEGIN}
# Auto-installed by `spec shell install`. Wraps `git init` so a fresh
# Spec bundle is scaffolded in the same directory immediately after the
# git worktree is created. Idempotent: skipped when `spec.yaml` already
# exists at the target. Remove this whole block (sentinels included)
# or run `spec shell uninstall` to opt out.
git() {{
  command git "$@"
  local __spec_rc=$?
  if [ "$1" = "init" ] && [ $__spec_rc -eq 0 ] && command -v spec >/dev/null 2>&1; then
    local __spec_target="$PWD"
    local __spec_skip=0
    local __spec_arg
    for __spec_arg in "$@"; do
      case "$__spec_arg" in
        --bare|--shared=*) __spec_skip=1 ;;
        init|-*) ;;
        *) __spec_target="$__spec_arg" ;;
      esac
    done
    if [ $__spec_skip -eq 0 ] && [ -d "$__spec_target" ] && [ ! -f "$__spec_target/spec.yaml" ]; then
      ( cd "$__spec_target" && spec init )
    fi
  fi
  return $__spec_rc
}}
{SHELL_INTEGRATION_END}
"""


# Fish has its own grammar; we ship the same semantics rewritten in
# fish so users on fish aren't second-class.
SHELL_INTEGRATION_BODY_FISH: str = f"""\
{SHELL_INTEGRATION_BEGIN}
# Auto-installed by `spec shell install`. Wraps `git init` so a fresh
# Spec bundle is scaffolded in the same directory immediately after the
# git worktree is created. Run `spec shell uninstall` to remove.
function git
    command git $argv
    set -l __spec_rc $status
    if test (count $argv) -ge 1; and test "$argv[1]" = init; and test $__spec_rc -eq 0; and type -q spec
        set -l __spec_target $PWD
        set -l __spec_skip 0
        for __spec_arg in $argv
            switch $__spec_arg
                case --bare '--shared=*'
                    set __spec_skip 1
                case init '-*'
                case '*'
                    set __spec_target $__spec_arg
            end
        end
        if test $__spec_skip -eq 0; and test -d $__spec_target; and not test -f $__spec_target/spec.yaml
            pushd $__spec_target
            spec init
            popd
        end
    end
    return $__spec_rc
end
{SHELL_INTEGRATION_END}
"""


def _detect_shell_kind(explicit: str | None) -> str:
    """Resolve which shell flavour to install for.

    ``explicit`` (from ``--shell``) wins; otherwise we read ``$SHELL``.
    Falls back to ``zsh`` (the macOS default since Catalina) when we
    genuinely can't tell.
    """
    if explicit:
        kind = explicit.strip().lower()
        if kind not in ("bash", "zsh", "fish"):
            fatal(f"Unsupported shell: {explicit}. Use one of: bash, zsh, fish.")
        return kind
    shell = os.environ.get("SHELL", "").strip()
    base = Path(shell).name if shell else ""
    if base == "fish":
        return "fish"
    if base == "bash":
        return "bash"
    if base == "zsh":
        return "zsh"
    return "zsh"


def _default_rc_file(shell_kind: str) -> Path:
    """Pick a sensible rc file per shell flavour.

    For bash on macOS the conventional file is ``~/.bash_profile`` (login
    shell), but ``~/.bashrc`` is what people on Linux expect. We prefer
    whichever already exists; if neither does, default to ``~/.bashrc``
    so subsequent installs are idempotent.
    """
    home = Path.home()
    if shell_kind == "zsh":
        return home / ".zshrc"
    if shell_kind == "fish":
        return home / ".config" / "fish" / "config.fish"
    bashrc = home / ".bashrc"
    bash_profile = home / ".bash_profile"
    if bashrc.exists():
        return bashrc
    if bash_profile.exists():
        return bash_profile
    return bashrc


def _body_for_shell(shell_kind: str) -> str:
    if shell_kind == "fish":
        return SHELL_INTEGRATION_BODY_FISH
    return SHELL_INTEGRATION_BODY_BASH_ZSH


def _install_shell_block(rc_path: Path, body: str) -> tuple[str, Path]:
    """Install or update the Spec block in ``rc_path``.

    Returns ``(status, path)`` where ``status`` is one of:
        ``installed``  — fresh rc file written
        ``appended``   — block added to an existing user-authored file
        ``updated``    — replaced an existing Spec block in place
        ``unchanged``  — block already matches; no write performed
    """
    rc_path.parent.mkdir(parents=True, exist_ok=True)

    if not rc_path.exists():
        rc_path.write_text(body, encoding="utf-8")
        return "installed", rc_path

    existing = rc_path.read_text(encoding="utf-8")
    if SHELL_INTEGRATION_BEGIN in existing and SHELL_INTEGRATION_END in existing:
        start = existing.index(SHELL_INTEGRATION_BEGIN)
        end = existing.index(SHELL_INTEGRATION_END) + len(SHELL_INTEGRATION_END)
        updated = existing[:start] + body.rstrip() + existing[end:]
        if updated == existing:
            return "unchanged", rc_path
        rc_path.write_text(updated, encoding="utf-8")
        return "updated", rc_path

    separator = "" if existing.endswith("\n") else "\n"
    rc_path.write_text(existing + separator + "\n" + body, encoding="utf-8")
    return "appended", rc_path


def _uninstall_shell_block(rc_path: Path) -> tuple[str, Path]:
    """Strip the Spec block from ``rc_path``.

    Returns ``(status, path)`` where ``status`` is ``missing`` (file
    does not exist), ``no_spec_block`` (file present, nothing to remove),
    or ``stripped`` (block removed; rest of file preserved).
    """
    if not rc_path.is_file():
        return "missing", rc_path
    try:
        existing = rc_path.read_text(encoding="utf-8")
    except OSError:
        return "no_spec_block", rc_path
    if SHELL_INTEGRATION_BEGIN not in existing or SHELL_INTEGRATION_END not in existing:
        return "no_spec_block", rc_path
    start = existing.index(SHELL_INTEGRATION_BEGIN)
    end = existing.index(SHELL_INTEGRATION_END) + len(SHELL_INTEGRATION_END)
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
    out = updated if (not updated or updated.endswith("\n")) else updated + "\n"
    rc_path.write_text(out, encoding="utf-8")
    return "stripped", rc_path


@click.group(
    "shell",
    help=(
        "Manage the `git init` → `spec init` shell wrapper "
        "(installed by the curl installer; commands here are for review, "
        "manual installs, switching shells, and uninstall)."
    ),
)
def shell_group() -> None:
    pass


@shell_group.command("install")
@click.option(
    "--shell",
    "shell_flag",
    type=click.Choice(["bash", "zsh", "fish"], case_sensitive=False),
    default=None,
    help="Force a specific shell flavour. Defaults to detection from $SHELL.",
)
@click.option(
    "--rc-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Override which rc file to write to. Default: ~/.zshrc, ~/.bashrc, "
    "or ~/.config/fish/config.fish depending on the shell.",
)
def shell_install_cmd(shell_flag: str | None, rc_file: Path | None) -> None:
    """Install the `git init` → `spec init` shell wrapper into your rc file."""
    kind = _detect_shell_kind(shell_flag)
    rc_path = (rc_file.expanduser().resolve() if rc_file else _default_rc_file(kind))
    body = _body_for_shell(kind)

    try:
        status, path = _install_shell_block(rc_path, body)
    except OSError as e:
        fatal(f"Could not write {rc_path}: {e}")
        return

    ok(f"Spec shell integration {status} for {kind}.")
    pointer("rc file     ", str(path))
    info("")
    dim("New shells will pick this up automatically. To activate now:")
    if kind == "fish":
        dim(f"  source {path}")
    else:
        dim(f"  source {path}")
    info("")
    dim(
        "From now on, `git init` (or `git init <dir>`) also runs `spec init` "
        "in the new repo. Skipped when `spec.yaml` already exists or for "
        "`--bare` repos."
    )
    dim("Remove later with: spec shell uninstall")


@shell_group.command("uninstall")
@click.option(
    "--shell",
    "shell_flag",
    type=click.Choice(["bash", "zsh", "fish"], case_sensitive=False),
    default=None,
    help="Force a specific shell flavour. Defaults to detection from $SHELL.",
)
@click.option(
    "--rc-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Override which rc file to strip. Default matches `spec shell install`.",
)
def shell_uninstall_cmd(shell_flag: str | None, rc_file: Path | None) -> None:
    """Remove the Spec shell integration block from your rc file."""
    kind = _detect_shell_kind(shell_flag)
    rc_path = (rc_file.expanduser().resolve() if rc_file else _default_rc_file(kind))

    try:
        status, path = _uninstall_shell_block(rc_path)
    except OSError as e:
        fatal(f"Could not write {rc_path}: {e}")
        return

    if status == "missing":
        dim(f"No rc file at {path} — nothing to remove.")
        return
    if status == "no_spec_block":
        dim(f"No Spec block found in {path} — nothing to remove.")
        return
    ok(f"Spec shell integration removed from {path}.")
    info("")
    dim("Open a new shell (or re-source the file) to drop the wrapper.")


@shell_group.command("snippet")
@click.option(
    "--shell",
    "shell_flag",
    type=click.Choice(["bash", "zsh", "fish"], case_sensitive=False),
    default=None,
    help="Print the snippet for a specific shell flavour. Defaults to "
    "detection from $SHELL.",
)
def shell_snippet_cmd(shell_flag: str | None) -> None:
    """Print the wrapper snippet to stdout (for manual installs / review)."""
    kind = _detect_shell_kind(shell_flag)
    click.echo(_body_for_shell(kind), nl=False)


__all__ = [
    "SHELL_INTEGRATION_BEGIN",
    "SHELL_INTEGRATION_BODY_BASH_ZSH",
    "SHELL_INTEGRATION_BODY_FISH",
    "SHELL_INTEGRATION_END",
    "shell_group",
]
