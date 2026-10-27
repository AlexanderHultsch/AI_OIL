from concurrent.futures import ThreadPoolExecutor

import pytest
import sqlalchemy
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from ai_oil.db.engine import create_db_engine
from ai_oil.domain.projects import allocate_issue_number, create_project, get_project_by_id

THREADS = 4
ITERATIONS = 25


def test_write_transactions_begin_immediate(engine: sqlalchemy.Engine, tmp_path) -> None:
    other = create_db_engine(tmp_path / "test.db")

    @event.listens_for(other, "connect")
    def _short_busy_timeout(dbapi_connection, connection_record) -> None:
        dbapi_connection.execute("PRAGMA busy_timeout=100")

    try:
        with engine.begin() as holder:
            holder.execute(text("SELECT count(*) FROM project")).scalar_one()
            with pytest.raises(OperationalError, match="database is locked"):
                with other.begin() as writer:
                    writer.execute(text("INSERT INTO label (name) VALUES ('blocked')"))
    finally:
        other.dispose()


def test_issue_numbers_unique_and_gapless_under_concurrency(engine: sqlalchemy.Engine) -> None:
    with engine.begin() as setup:
        project_id = create_project(setup, "OIL", "Open issues").id

    def allocate_many() -> list[int]:
        numbers = []
        for _ in range(ITERATIONS):
            with engine.begin() as connection:
                numbers.append(allocate_issue_number(connection, project_id))
        return numbers

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [pool.submit(allocate_many) for _ in range(THREADS)]
        results = [number for future in futures for number in future.result()]

    assert sorted(results) == list(range(1, THREADS * ITERATIONS + 1))
    with engine.begin() as check:
        assert get_project_by_id(check, project_id).next_number == THREADS * ITERATIONS + 1
