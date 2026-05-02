"""Entry point — wires every subcommand into a single `spec` binary."""

from __future__ import annotations

import click

from . import __version__
from .commands.add import add_cmd
from .commands.compile import compile_cmd
from .commands.git_hooks import git_hooks_group
from .commands.init import init_cmd
from .commands.log import log_cmd
from .commands.login import login_cmd, logout_cmd
from .commands.prompts import prompts_group
from .commands.pull import pull_cmd
from .commands.push import push_cmd
from .commands.unstage import unstage_cmd
from .commands.status import status_cmd


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 100}


@click.group(
    context_settings=CONTEXT_SETTINGS,
    help=(
        "Spec — governed bundles of plain-English source code.\n\n"
        "Author intent (`.md`), capture prompt history (`.prompt`), compile with Claude Code."
    ),
)
@click.version_option(__version__, "-V", "--version", prog_name="spec")
def cli() -> None:
    pass


cli.add_command(init_cmd)
cli.add_command(git_hooks_group)
cli.add_command(login_cmd)
cli.add_command(logout_cmd)
cli.add_command(status_cmd)
cli.add_command(add_cmd)
cli.add_command(unstage_cmd)
cli.add_command(push_cmd)
cli.add_command(pull_cmd)
cli.add_command(compile_cmd)
cli.add_command(prompts_group)
cli.add_command(log_cmd)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
