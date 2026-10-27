"""Entry point of the `oil` command."""

import argparse

from ai_oil import __version__

COMMAND_MODULES: list = []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oil", description="AI_OIL — AI-assisted open issue list")
    parser.add_argument("--version", action="version", version=f"oil {__version__}")
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
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
