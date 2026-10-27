"""Entry point of the `oil` command."""

import argparse
import sys
from pathlib import Path

from ai_oil import __version__
from ai_oil.cli import backup as backup_command
from ai_oil.cli import comment as comment_command
from ai_oil.cli import init as init_command
from ai_oil.cli import issue as issue_command
from ai_oil.cli import issue_link as issue_link_command
from ai_oil.cli import person as person_command
from ai_oil.cli import project as project_command
from ai_oil.cli import search as search_command
from ai_oil.cli import source as source_command
from ai_oil.errors import AiOilError

COMMAND_MODULES: list = [
    init_command,
    project_command,
    issue_command,
    comment_command,
    issue_link_command,
    person_command,
    search_command,
    source_command,
    backup_command,
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oil", description="AI_OIL — AI-assisted open issue list")
    parser.add_argument("--version", action="version", version=f"oil {__version__}")
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="data folder (default: AI_OIL_HOME or %%LOCALAPPDATA%%\\AI_OIL)",
    )
    subparsers = parser.add_subparsers(dest="command")
    for module in COMMAND_MODULES:
        module.register(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except AiOilError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
