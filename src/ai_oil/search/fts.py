"""Full-text search over issues, comments and source content (SRCH-1 .. SRCH-3)."""

from dataclasses import dataclass
from pathlib import Path

import sqlalchemy

from ai_oil.errors import ValidationError

KIND_CODES = {"issue": 1, "comment": 2, "source_version": 3, "source_entry": 4}
ROWID_BASE = 1_000_000_000_000

_TRIGGERS_SQL = Path(__file__).resolve().parents[1] / "db" / "sql" / "0002_search_triggers.sql"
_BACKFILL_BEGIN = "-- BEGIN BACKFILL"
_BACKFILL_END = "-- END BACKFILL"


@dataclass(frozen=True)
class Hit:
    """One matching search document; `snippet` highlights the match in the body with [ ] (SRCH-3)."""

    doc_kind: str
    doc_id: int
    rank: float
    title: str
    snippet: str


def _quote(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


def to_fts_query(text: str) -> str:
    """Turn user text into a safe FTS5 MATCH expression.

    Words become quoted prefix tokens, "quoted segments" become phrases and the tokens are ANDed, so
    FTS5 syntax characters and the operator words AND, OR, NOT and NEAR are always literal (SRCH-1).
    """
    parts = text.split('"')
    last = len(parts) - 1
    terms: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 1 and index != last:
            phrase = " ".join(part.split())
            if phrase:
                terms.append(_quote(phrase))
        else:
            terms.extend(_quote(word) + "*" for word in part.split())
    if not terms:
        raise ValidationError("empty search text")
    return " ".join(terms)


def search_docs(conn: sqlalchemy.Connection, text: str, *, limit: int = 50) -> list[Hit]:
    """Best matches first; the snippet comes from the body column (SRCH-1, SRCH-3)."""
    rows = conn.execute(
        sqlalchemy.text(
            "SELECT doc_kind, CAST(doc_id AS INTEGER) AS doc_id, rank, title,"
            " snippet(search_doc, 3, '[', ']', '…', 12) AS snippet"
            " FROM search_doc WHERE search_doc MATCH :q ORDER BY rank LIMIT :limit"
        ),
        {"q": to_fts_query(text), "limit": limit},
    ).mappings()
    return [Hit(**row) for row in rows]


_ISSUE_IDS_SQL = sqlalchemy.text(
    "WITH hit AS (SELECT doc_kind, CAST(doc_id AS INTEGER) AS doc_id FROM search_doc"
    " WHERE search_doc MATCH :q)"
    " SELECT doc_id AS issue_id FROM hit WHERE doc_kind = 'issue'"
    " UNION"
    " SELECT c.issue_id FROM hit JOIN comment c ON c.id = hit.doc_id"
    " WHERE hit.doc_kind = 'comment' AND c.deleted_at IS NULL"
    " UNION"
    " SELECT l.issue_id FROM hit"
    " JOIN source_version v ON v.id = hit.doc_id"
    " JOIN source_object o ON o.id = v.source_object_id"
    " JOIN issue_source_link l ON l.source_object_id = coalesce(o.parent_id, o.id) AND l.removed_at IS NULL"
    " WHERE hit.doc_kind = 'source_version'"
    " UNION"
    " SELECT l.issue_id FROM hit"
    " JOIN source_entry e ON e.id = hit.doc_id"
    " JOIN source_object o ON o.id = e.source_object_id"
    " JOIN issue_source_link l ON l.source_object_id = coalesce(o.parent_id, o.id) AND l.removed_at IS NULL"
    " WHERE hit.doc_kind = 'source_entry'"
)


def issue_ids_matching(conn: sqlalchemy.Connection, text: str) -> set[int]:
    """Ids of the issues the text matches directly, through a comment or through a linked source (SRCH-2).

    A child object (attachment) counts for the issues linked to its parent (DM-44).
    """
    return set(conn.execute(_ISSUE_IDS_SQL, {"q": to_fts_query(text)}).scalars())


def _backfill_statements() -> list[str]:
    script = _TRIGGERS_SQL.read_text(encoding="utf-8")
    section = script.split(_BACKFILL_BEGIN, 1)[1].split(_BACKFILL_END, 1)[0]
    lines = [line for line in section.splitlines() if not line.lstrip().startswith("--")]
    return [statement.strip() for statement in "\n".join(lines).split(";") if statement.strip()]


def reindex_all(conn: sqlalchemy.Connection) -> dict[str, int]:
    """Rebuild the whole index from the mirrored tables; returns the document count per kind."""
    conn.execute(sqlalchemy.text("DELETE FROM search_doc"))
    for statement in _backfill_statements():
        conn.execute(sqlalchemy.text(statement))
    counts = dict.fromkeys(KIND_CODES, 0)
    for kind, count in conn.execute(
        sqlalchemy.text("SELECT doc_kind, count(*) FROM search_doc GROUP BY doc_kind")
    ):
        counts[kind] = count
    return counts
