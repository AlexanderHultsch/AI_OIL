"""`oil setup`: the interactive setup wizard (docs/installer_concept.md)."""

import argparse

from ai_oil.config import default_home
from ai_oil.setup.console import Console
from ai_oil.setup.model import SetupContext
from ai_oil.setup.probe import LocalProbe
from ai_oil.setup.wizard import STAGES, run_wizard


def register(subparsers) -> None:
    parser = subparsers.add_parser("setup", help="interactive setup wizard")
    parser.add_argument("--only", metavar="STAGE", default=None, help="run just this stage (see --list)")
    parser.add_argument("--list", action="store_true", help="list the stages and exit")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    if args.list:
        for stage in STAGES:
            print(f"{stage.name}  {stage.title}")
        return 0
    ctx = SetupContext(home=args.home or default_home(), console=Console(), probe=LocalProbe(), args=args)
    try:
        return run_wizard(ctx, only=args.only)
    except KeyboardInterrupt:
        print("Setup aborted — nothing half-written; run `oil setup` again any time.")
        return 1
    finally:
        ctx.close()
