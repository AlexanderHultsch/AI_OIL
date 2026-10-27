"""Baseline: executes 0001_baseline.sql as one script.

Revision ID: 0001
Revises:
"""

from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

BASELINE_SQL = Path(__file__).resolve().parents[2] / "sql" / "0001_baseline.sql"


def upgrade() -> None:
    sql = BASELINE_SQL.read_text(encoding="utf-8")
    raw = op.get_bind().connection.driver_connection
    # The script contains triggers with inner semicolons, so it must not be split.
    # executescript() manages its own transaction; BEGIN/COMMIT keeps the baseline atomic.
    raw.executescript(f"BEGIN;\n{sql}\nCOMMIT;")


def downgrade() -> None:
    raise NotImplementedError("the baseline cannot be downgraded")
