"""`oil backup` and `oil restore` (NFR-8)."""

import argparse
from pathlib import Path

from ai_oil.backup import create_backup, restore_backup
from ai_oil.cli.context import open_context
from ai_oil.config import load_config


def register(subparsers) -> None:
    backup = subparsers.add_parser("backup", help="write a consistent backup of the database")
    backup.add_argument("--to", type=Path, default=None, metavar="PATH", help="backup folder")
    backup.set_defaults(func=run_backup)

    restore = subparsers.add_parser("restore", help="replace the database with a backup")
    restore.add_argument("path", metavar="PATH", type=Path, help="backup file")
    restore.add_argument("--yes", action="store_true", help="confirm the replacement")
    restore.set_defaults(func=run_restore)


def run_backup(args: argparse.Namespace) -> int:
    ctx = open_context(args)
    try:
        path = create_backup(ctx.engine, ctx.config, folder=args.to)
    finally:
        ctx.engine.dispose()
    print(f"Backup written: {path}")
    return 0


def run_restore(args: argparse.Namespace) -> int:
    config = load_config(args.home)
    if not args.yes:
        print(
            f"This replaces {config.db_path} with {args.path}. The current database is kept as a safety copy."
            " Re-run with --yes to proceed."
        )
        return 1
    safety = restore_backup(config, args.path)
    print(f"Restored {args.path}; previous database kept at {safety}")
    return 0
