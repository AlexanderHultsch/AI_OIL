"""Search index triggers: executes 0002_search_triggers.sql as one script.

Revision ID: 0002
Revises: 0001
"""

from pathlib import Path

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SEARCH_TRIGGERS_SQL = Path(__file__).resolve().parents[2] / "sql" / "0002_search_triggers.sql"


def upgrade() -> None:
    sql = SEARCH_TRIGGERS_SQL.read_text(encoding="utf-8")
    raw = op.get_bind().connection.driver_connection
    # The script contains triggers with inner semicolons, so it must not be split.
    # executescript() manages its own transaction; BEGIN/COMMIT keeps the migration atomic.
    raw.executescript(f"BEGIN;\n{sql}\nCOMMIT;")


def downgrade() -> None:
    raise NotImplementedError("the search triggers cannot be downgraded")
