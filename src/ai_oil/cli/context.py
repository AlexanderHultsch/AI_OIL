"""Shared context for CLI commands: configuration and an up-to-date database engine."""

import argparse
import sys
from dataclasses import dataclass

import sqlalchemy

from ai_oil.config import Config, load_config
from ai_oil.db.engine import create_db_engine
from ai_oil.db.migrate import upgrade
from ai_oil.setup.probe import LocalProbe, inside_onedrive


@dataclass
class CliContext:
    """Configuration and engine for one CLI invocation."""

    config: Config
    engine: sqlalchemy.Engine


def open_context(args: argparse.Namespace) -> CliContext:
    """Load the configuration, open the database and migrate it to the latest schema."""
    config = load_config(args.home)
    root = inside_onedrive(config.home, LocalProbe().onedrive_roots())
    if root is not None:
        print(
            f"warning: the AI_OIL data folder {config.home} is inside OneDrive ({root})"
            " — move it out (see docs/install_windows.md)",
            file=sys.stderr,
        )
    engine = create_db_engine(config.db_path)
    upgrade(engine)
    return CliContext(config=config, engine=engine)
