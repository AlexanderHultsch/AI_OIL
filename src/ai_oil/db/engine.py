"""SQLAlchemy engine factory. Every connection enforces foreign keys and runs in WAL mode."""

from pathlib import Path

import sqlalchemy
from sqlalchemy import event


def create_db_engine(path: str | Path) -> sqlalchemy.Engine:
    """Create the engine for the database file at `path`; the parent directory is created if missing."""
    db_path = Path(path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = sqlalchemy.create_engine(f"sqlite+pysqlite:///{db_path}")

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    return engine
