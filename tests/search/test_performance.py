"""SRCH-9: < 300 ms without `text ~` and < 1 s with it, at 1,000 issues and 100,000 entries."""

import random
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy
from sqlalchemy import text

from ai_oil.db.engine import create_db_engine
from ai_oil.db.migrate import upgrade
from ai_oil.domain import people, projects
from ai_oil.search import compiler

pytestmark = pytest.mark.performance

ISSUES = 1_000
OBJECTS = 2_000
ENTRIES_PER_OBJECT = 50
VOCABULARY_SIZE = 4_300
WORDS_PER_ENTRY = 43  # 43 six-letter words and 42 blanks are 300 characters
BATCH = 5_000

STRUCTURED_LIMIT = 0.300
TEXT_LIMIT = 1.0

STRUCTURED_QUERY = 'status IN ("In Progress", Blocked) AND priority IN (P1, P2) ORDER BY due'

_STATUSES = ("New", "Ready to Proceed", "In Progress", "Blocked", "Done", "Cancelled")
_STATUS_WEIGHTS = (20, 10, 30, 10, 25, 5)
_PRIORITIES = ("P1", "P2", "P3", "P4", "P5")
_PRIORITY_WEIGHTS = (10, 20, 35, 25, 10)
_TYPES = ("Epic", "Task", "Task", "Task", "Sub-task")
_PERSONS = ("Alex Hultsch", "Bea Brandt", "Carl Conrad", "Dana Dietz", "Emil Eberle")
_LABELS = tuple(f"label{n}" for n in range(10))
_CONSONANTS = "bcdfghklmnprstv"
_VOWELS = "aeiou"


@dataclass(frozen=True)
class Dataset:
    conn: sqlalchemy.Connection
    rare_word: str
    rare_share: float


def _insert(conn: sqlalchemy.Connection, statement: str, rows: list[dict[str, object]]) -> None:
    for start in range(0, len(rows), BATCH):
        conn.execute(text(statement), rows[start : start + BATCH])


def _vocabulary(rng: random.Random) -> list[str]:
    """Distinct six-letter words of one shape, so a prefix match on a word finds only that word."""
    syllables = [c + v for c in _CONSONANTS for v in _VOWELS]
    words = {"".join(rng.choices(syllables, k=3)) for _ in range(VOCABULARY_SIZE * 2)}
    return sorted(words)[:VOCABULARY_SIZE]


def _timestamp(rng: random.Random, month: int) -> str:
    return (
        f"2026-{month:02d}-{rng.randint(1, 28):02d}T{rng.randint(6, 18):02d}:{rng.randint(0, 59):02d}:00.000Z"
    )


def _build_issues(conn: sqlalchemy.Connection, rng: random.Random, person_ids: list[int]) -> None:
    rows: list[dict[str, object]] = []
    for number in range(1, ISSUES + 1):
        status = rng.choices(_STATUSES, _STATUS_WEIGHTS)[0]
        due = f"{rng.choice((2026, 2027))}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        has_due = rng.random() < 0.8
        created = _timestamp(rng, rng.randint(1, 9))
        rows.append(
            {
                "id": number,
                "project_id": 1,
                "number": number,
                "key": f"PERF-{number}",
                "issue_type": rng.choice(_TYPES),
                "summary": f"Perf issue {number} for the supplier review",
                "description": f"Description of perf issue {number}",
                "status": status,
                "blocked_reason": "waiting for samples" if status == "Blocked" else None,
                "cancel_reason": "not needed" if status == "Cancelled" else None,
                "priority": rng.choices(_PRIORITIES, _PRIORITY_WEIGHTS)[0],
                "assignee_id": rng.choice(person_ids) if rng.random() < 0.9 else None,
                "reporter_id": person_ids[0],
                "due": due if has_due else None,
                "created_at": created,
                "updated_at": _timestamp(rng, rng.randint(10, 12)),
                "resolved_at": created if status in ("Done", "Cancelled") else None,
            }
        )
    _insert(
        conn,
        "INSERT INTO issue (id, project_id, number, key, issue_type, summary, description, status,"
        " blocked_reason, cancel_reason, priority, assignee_id, reporter_id, original_due_date,"
        " revised_due_date, created_at, updated_at, resolved_at)"
        " VALUES (:id, :project_id, :number, :key, :issue_type, :summary, :description, :status,"
        " :blocked_reason, :cancel_reason, :priority, :assignee_id, :reporter_id, :due, :due,"
        " :created_at, :updated_at, :resolved_at)",
        rows,
    )
    conn.execute(text("UPDATE project SET next_number = :next WHERE id = 1"), {"next": ISSUES + 1})
    _insert(
        conn,
        "INSERT INTO label (id, name) VALUES (:id, :name)",
        [{"id": n + 1, "name": name} for n, name in enumerate(_LABELS)],
    )
    _insert(
        conn,
        "INSERT INTO issue_label (issue_id, label_id) VALUES (:issue_id, :label_id)",
        [
            {"issue_id": number, "label_id": label + 1}
            for number in range(1, ISSUES + 1)
            for label in rng.sample(range(len(_LABELS)), rng.randint(0, 3))
        ],
    )


def _build_sources(conn: sqlalchemy.Connection, rng: random.Random) -> tuple[str, float]:
    conn.execute(text("INSERT INTO connector (id, type, name) VALUES (1, 'outlook', 'Outlook perf')"))
    _insert(
        conn,
        "INSERT INTO source_object (id, connector_id, object_type, external_id, title, display_ref,"
        " last_changed_at) VALUES (:id, 1, 'email_thread', :external_id, :title, :title, :changed)",
        [
            {
                "id": n,
                "external_id": f"thread-{n}",
                "title": f"Thread {n} supplier quotation",
                "changed": _timestamp(rng, rng.randint(9, 12)),
            }
            for n in range(1, OBJECTS + 1)
        ],
    )
    _insert(
        conn,
        "INSERT INTO issue_source_link (issue_id, source_object_id, role, link_method)"
        " VALUES (:issue_id, :source_object_id, 'reference', 'manual')",
        [
            {"issue_id": issue_id, "source_object_id": n}
            for n in range(1, OBJECTS + 1)
            for issue_id in rng.sample(range(1, ISSUES + 1), rng.randint(1, 3))
        ],
    )

    vocabulary = _vocabulary(rng)
    document_frequency: Counter[str] = Counter()
    entries: list[dict[str, object]] = []
    for n in range(1, OBJECTS + 1):
        for k in range(ENTRIES_PER_OBJECT):
            words = rng.choices(vocabulary, k=WORDS_PER_ENTRY)
            document_frequency.update(set(words))
            entries.append(
                {
                    "object_id": n,
                    "external_id": f"msg-{n}-{k}",
                    "occurred_at": _timestamp(rng, rng.randint(9, 12)),
                    "text": " ".join(words),
                }
            )
    total = OBJECTS * ENTRIES_PER_OBJECT
    rare_word = min(sorted(document_frequency), key=lambda word: abs(document_frequency[word] - total * 0.01))
    _insert(
        conn,
        "INSERT INTO source_entry"
        " (source_object_id, entry_type, external_id, occurred_at, text, content_hash)"
        " VALUES (:object_id, 'email_message', :external_id, :occurred_at, :text, 'h')",
        entries,
    )
    return rare_word, document_frequency[rare_word] / total


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Dataset]:
    engine = create_db_engine(Path(tmp_path_factory.mktemp("perf")) / "perf.db")
    upgrade(engine)
    rng = random.Random(42)
    with engine.begin() as conn:
        person_ids = [
            people.create_person(conn, name, is_me=index == 0).id for index, name in enumerate(_PERSONS)
        ]
        projects.create_project(conn, "PERF", "Performance")
        _build_issues(conn, rng, person_ids)
        rare_word, rare_share = _build_sources(conn, rng)
    with engine.connect() as conn:
        yield Dataset(conn, rare_word, rare_share)
    engine.dispose()


def _best_of_three(conn: sqlalchemy.Connection, query: str) -> tuple[float, int]:
    best = float("inf")
    count = 0
    for _ in range(3):
        started = time.perf_counter()
        found = compiler.search_issues(conn, query)
        best = min(best, time.perf_counter() - started)
        count = len(found)
    return best, count


def test_dataset_has_the_specified_size(dataset: Dataset) -> None:
    counts = {
        table: dataset.conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        for table in ("issue", "source_object", "source_entry")
    }
    assert counts == {
        "issue": ISSUES,
        "source_object": OBJECTS,
        "source_entry": OBJECTS * ENTRIES_PER_OBJECT,
    }
    assert 0.005 <= dataset.rare_share <= 0.02


def test_structured_query_is_fast(dataset: Dataset) -> None:
    seconds, count = _best_of_three(dataset.conn, STRUCTURED_QUERY)

    print(
        f"\nSRCH-9 structured: {seconds * 1000:.1f} ms for {count} issues"
        f" (limit {STRUCTURED_LIMIT * 1000:.0f} ms)"
    )
    assert count > 0
    assert seconds < STRUCTURED_LIMIT


def test_text_query_is_fast(dataset: Dataset) -> None:
    query = f'text ~ "{dataset.rare_word}" AND statusCategory != Done'

    seconds, count = _best_of_three(dataset.conn, query)

    print(
        f"\nSRCH-9 text: {seconds * 1000:.1f} ms for {count} issues,"
        f" word {dataset.rare_word!r} in {dataset.rare_share:.2%} of entries"
        f" (limit {TEXT_LIMIT * 1000:.0f} ms)"
    )
    assert count > 0
    assert seconds < TEXT_LIMIT
