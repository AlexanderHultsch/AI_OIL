from collections.abc import Iterator

import pytest
import sqlalchemy

from ai_oil.db.engine import create_db_engine
from ai_oil.db.migrate import upgrade


@pytest.fixture
def engine(tmp_path) -> Iterator[sqlalchemy.Engine]:
    db_engine = create_db_engine(tmp_path / "test.db")
    upgrade(db_engine)
    yield db_engine
    db_engine.dispose()
