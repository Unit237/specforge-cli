"""Entry point — wires every subcommand into a single `spec` binary."""

from __future__ import annotations

from copy import copy

import click

from . import __version__
from .commands.add import add_cmd
from .commands.bundle import bundle_group
from .commands.compile import compile_cmd
from .commands.codex import codex_group
from .commands.discover import discover_cmd
from .commands.git_hooks import git_hooks_group
from .commands.hooks import hooks_group
from .commands.init import init_cmd
from .commands.journal import journal_group
from .commands.live import live_group
from .commands.locks import locks_group
from .commands.log import log_cmd
from .commands.login import login_cmd, logout_cmd
from .commands.presence import presence_group
from .commands.prompts import prompts_group
from .commands.pull import pull_cmd
from .commands.review import prs_cmd, review_cmd
from .commands.push import push_cmd
from .commands.shell import shell_group
from .commands.team import team_cmd, team_watch_cmd
from .commands.unstage import unstage_cmd
from .commands.status import status_cmd
from .commands.watch import watch_cmd
from .commands.workday import workday_off_cmd, workday_on_cmd


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 100}


@click.group(
    context_settings=CONTEXT_SETTINGS,
    help=(
        "Spec — governed bundles of plain-English source code.\n\n"
        "Author intent (`.md`), capture prompt history (`.prompts`), compile with "
        "your coding agent or configured model API."
    ),
)
@click.version_option(__version__, "-V", "--version", prog_name="spec")
def cli() -> None:
    pass


cli.add_command(init_cmd)
cli.add_command(discover_cmd)
cli.add_command(bundle_group)
cli.add_command(git_hooks_group)
cli.add_command(shell_group)
cli.add_command(login_cmd)
cli.add_command(logout_cmd)
cli.add_command(status_cmd)
cli.add_command(add_cmd)
cli.add_command(unstage_cmd)
cli.add_command(push_cmd, name="publish")
push_compat_cmd = copy(push_cmd)
push_compat_cmd.hidden = True
cli.add_command(push_compat_cmd, name="push")
cli.add_command(pull_cmd)
cli.add_command(review_cmd)
cli.add_command(prs_cmd)
cli.add_command(compile_cmd)
cli.add_command(codex_group)
cli.add_command(prompts_group)
cli.add_command(log_cmd)
cli.add_command(watch_cmd)
cli.add_command(team_cmd)
activity_cmd = copy(team_watch_cmd)
activity_cmd.help = """Stream every agent turn across your Spec workspace.

Shows prompts and assistant prose from Codex, Claude Code, Cursor, and
Compress across every bundle you can access. Tool calls and large code blocks
stay collapsed by default; add ``--show-tool-runs`` when you need the longer
execution trace. This is receive-only and works outside a bundle directory.

Examples:

\b
  spec activity
  spec activity --show-tool-runs

The compatible advanced spelling is ``spec team watch``.
"""
cli.add_command(activity_cmd, name="activity")
cli.add_command(journal_group)
cli.add_command(locks_group)
cli.add_command(live_group)
cli.add_command(presence_group)
cli.add_command(hooks_group)
cli.add_command(workday_on_cmd)
cli.add_command(workday_off_cmd)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
