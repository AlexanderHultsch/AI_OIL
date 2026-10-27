"""Programmatic Alembic runner; no alembic.ini is used."""

from pathlib import Path

import sqlalchemy
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import OperationalError

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def upgrade(engine: sqlalchemy.Engine) -> None:
    """Bring the database to the latest schema revision (`upgrade head`)."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def current_schema_version(conn: sqlalchemy.Connection) -> int:
    """Highest version in `schema_version`; 0 if the table does not exist yet."""
    try:
        version = conn.execute(sqlalchemy.text("SELECT max(version) FROM schema_version")).scalar()
    except OperationalError:
        return 0
    return version or 0
