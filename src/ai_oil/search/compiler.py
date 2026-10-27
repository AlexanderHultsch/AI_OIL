"""OQL to parameterised SQL (SRCH-4: no user text is ever concatenated into SQL)."""

import calendar
import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

import sqlalchemy

from ai_oil import timeutil
from ai_oil.domain import issues, people
from ai_oil.errors import NotFoundError, ValidationError
from ai_oil.search import fts, oql

_CATEGORIES = ("To Do", "In Progress", "Done")
_SOURCE_TYPES = {
    "email": "email_thread",
    "jira": "jira_issue",
    "confluence": "confluence_page",
    "sharepoint": "file",
    "note": "note",
    "web": "web_link",
}
_UNAVAILABLE = {"hasproposal": "phase 3", "waitingonme": "phase 4"}

_EQ_IN = ("=", "!=", "in", "not in")
_EMPTY = ("is empty", "is not empty")
_COMPARE = ("=", "!=", ">", ">=", "<", "<=")
_ORDERING = (">", ">=", "<", "<=")
_SUBSTRING = ("~", "!~")
_NEGATIVE = frozenset({"!=", "not in", "!~"})
_SQL_COMPARE = {"=": "=", "!=": "<>", ">": ">", ">=": ">=", "<": "<", "<=": "<="}

_ESCAPE = "ESCAPE '\\'"
_DATE_LITERAL = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_RELATIVE = re.compile(r"([+-])([0-9]+)([dwm])")
_END_OF_DAY = time(23, 59, 59, 999_000)
_FTS_PARAMETER = re.compile(r":q\b")
_FTS_SUBQUERY = fts._ISSUE_IDS_SQL.text

_SOURCE_UPDATED = (
    "(SELECT max(o.last_changed_at) FROM issue_source_link l"
    " JOIN source_object o ON (o.id = l.source_object_id OR o.parent_id = l.source_object_id)"
    " WHERE l.issue_id = i.id AND l.removed_at IS NULL)"
)
_ACTIVE_LINKED_OBJECT = (
    "EXISTS (SELECT 1 FROM issue_source_link l JOIN source_object o ON o.id = l.source_object_id"
    " WHERE l.issue_id = i.id AND l.removed_at IS NULL AND {condition})"
)
_ISSUE_LINK_SQL = {
    "issueLink": (
        "EXISTS (SELECT 1 FROM issue_link k JOIN issue o ON"
        " (k.from_issue_id = i.id AND o.id = k.to_issue_id)"
        " OR (k.to_issue_id = i.id AND o.id = k.from_issue_id)"
        " WHERE o.key IN ({keys}))"
    ),
    "blocks": (
        "EXISTS (SELECT 1 FROM issue_link k JOIN issue o ON o.id = k.to_issue_id"
        " WHERE k.link_type = 'blocks' AND k.from_issue_id = i.id AND o.key IN ({keys}))"
    ),
    "blockedBy": (
        "EXISTS (SELECT 1 FROM issue_link k JOIN issue o ON o.id = k.from_issue_id"
        " WHERE k.link_type = 'blocks' AND k.to_issue_id = i.id AND o.key IN ({keys}))"
    ),
    "duplicates": (
        "EXISTS (SELECT 1 FROM issue_link k JOIN issue o ON o.id = k.to_issue_id"
        " WHERE k.link_type = 'duplicates' AND k.from_issue_id = i.id AND o.key IN ({keys}))"
    ),
}

_JOINS = {
    "p": "JOIN project p ON p.id = i.project_id",
    "asg": "LEFT JOIN person asg ON asg.id = i.assignee_id",
    "rep": "LEFT JOIN person rep ON rep.id = i.reporter_id",
}

# name -> (canonical name, unit of the optional offset argument or None)
_DATE_FUNCTIONS = {
    "now": ("now", None),
    "startofday": ("startOfDay", "d"),
    "endofday": ("endOfDay", "d"),
    "startofweek": ("startOfWeek", "w"),
    "endofweek": ("endOfWeek", "w"),
}


@dataclass(frozen=True)
class _Field:
    name: str
    kind: str
    ops: tuple[str, ...]
    column: str = ""
    allowed: tuple[str, ...] = ()


_FIELD_LIST = (
    _Field("project", "key", _EQ_IN, "p.key"),
    _Field("key", "key", _EQ_IN, "i.key"),
    _Field("type", "enum", _EQ_IN, "i.issue_type", issues.ISSUE_TYPES),
    _Field("status", "enum", _EQ_IN, "i.status", issues.STATUSES),
    _Field("statusCategory", "category", _EQ_IN, "i.status", _CATEGORIES),
    _Field("priority", "enum", _EQ_IN, "i.priority", issues.PRIORITIES),
    _Field("origin", "enum", _EQ_IN, "i.origin_type", issues.ORIGIN_TYPES),
    _Field("assignee", "person", _EQ_IN + _EMPTY, "i.assignee_id"),
    _Field("reporter", "person", _EQ_IN + _EMPTY, "i.reporter_id"),
    _Field("label", "label", _EQ_IN + _EMPTY),
    _Field("component", "component", _EQ_IN + _EMPTY),
    _Field("parent", "parent", _EQ_IN + _EMPTY),
    _Field("issueLink", "issueLink", ("=", "in")),
    _Field("blocks", "blocks", ("=", "in")),
    _Field("blockedBy", "blockedBy", ("=", "in")),
    _Field("duplicates", "duplicates", ("=", "in")),
    _Field("due", "date", _COMPARE + _EMPTY, "i.revised_due_date"),
    _Field("originalDue", "date", _COMPARE + _EMPTY, "i.original_due_date"),
    _Field("created", "timestamp", _COMPARE + _EMPTY, "i.created_at"),
    _Field("updated", "timestamp", _COMPARE + _EMPTY, "i.updated_at"),
    _Field("resolved", "timestamp", _COMPARE + _EMPTY, "i.resolved_at"),
    _Field("sourceUpdated", "timestamp", _ORDERING + _EMPTY, _SOURCE_UPDATED),
    _Field("summary", "like", _SUBSTRING, "i.summary"),
    _Field("description", "like", _SUBSTRING, "i.description"),
    _Field("comment", "comment", _SUBSTRING),
    _Field("text", "text", _SUBSTRING),
    _Field("source", "source", _EQ_IN, allowed=tuple(_SOURCE_TYPES)),
    _Field("sourceRef", "sourceRef", ("=", "!=", "~")),
)
FIELD_NAMES: tuple[str, ...] = tuple(field.name for field in _FIELD_LIST)
_BY_LOWER = {field.name.lower(): field for field in _FIELD_LIST}


def _rank(column: str, ordered: tuple[str, ...], rank_of: dict[str, int] | None = None) -> str:
    """A CASE expression ranking the fixed enum values; only whitelisted constants are embedded."""
    rank_of = rank_of or {value: position for position, value in enumerate(ordered)}
    branches = "".join(f" WHEN '{value}' THEN {rank_of[value]}" for value in ordered)
    return f"CASE {column}{branches} END"


_STATUS_CATEGORY_RANK = {
    status: _CATEGORIES.index(issues.STATUS_CATEGORY[status]) for status in issues.STATUSES
}


def _nulls_last(column: str) -> str:
    return f"{column} IS NULL, {column} {{d}}"


# field -> (joins, ORDER BY terms with {d} for the direction)
_ORDER_TERMS: dict[str, tuple[tuple[str, ...], str]] = {
    "project": (("p",), "p.key {d}"),
    "key": (("p",), "p.key {d}, i.number {d}"),
    "type": ((), _rank("i.issue_type", issues.ISSUE_TYPES) + " {d}"),
    "status": ((), _rank("i.status", issues.STATUSES) + " {d}"),
    "statusCategory": (
        (),
        _rank("i.status", issues.STATUSES, _STATUS_CATEGORY_RANK) + " {d}",
    ),
    "priority": ((), "i.priority {d}"),
    "assignee": (("asg",), _nulls_last("asg.display_name")),
    "reporter": (("rep",), _nulls_last("rep.display_name")),
    "due": ((), _nulls_last("i.revised_due_date")),
    "originalDue": ((), _nulls_last("i.original_due_date")),
    "created": ((), _nulls_last("i.created_at")),
    "updated": ((), _nulls_last("i.updated_at")),
    "resolved": ((), _nulls_last("i.resolved_at")),
    "summary": ((), "i.summary COLLATE NOCASE {d}"),
}
_ORDER_BY_LOWER = {name.lower(): name for name in _ORDER_TERMS}


@dataclass(frozen=True)
class CompiledQuery:
    """`sql` selects `i.id` in result order; every user-supplied value is a bound parameter (`:p0`, ...)."""

    sql: str
    params: dict[str, object]


def _utc_iso(local: datetime) -> str:
    """A naive local datetime as the stored UTC text `YYYY-MM-DDTHH:MM:SS.fffZ`."""
    utc = local.astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


def _add_months(value: datetime, months: int) -> datetime:
    year, month_index = divmod(value.year * 12 + value.month - 1 + months, 12)
    month = month_index + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _suggest(word: str, candidates: list[str]) -> str | None:
    close = difflib.get_close_matches(word.lower(), [c.lower() for c in candidates], n=1, cutoff=0.6)
    if not close:
        return None
    return next(c for c in candidates if c.lower() == close[0])


class _Compiler:
    def __init__(self, conn: sqlalchemy.Connection) -> None:
        self._conn = conn
        self._now = timeutil._now().astimezone().replace(tzinfo=None)
        self._params: dict[str, object] = {}
        self._joins: set[str] = set()
        self._handlers = {
            "key": self._key,
            "enum": self._enum,
            "category": self._category,
            "person": self._person_clause,
            "label": self._label,
            "component": self._component,
            "parent": self._parent,
            "issueLink": self._issue_link,
            "blocks": self._issue_link,
            "blockedBy": self._issue_link,
            "duplicates": self._issue_link,
            "date": self._date,
            "timestamp": self._timestamp,
            "like": self._like,
            "comment": self._comment,
            "text": self._text,
            "source": self._source,
            "sourceRef": self._source_ref,
        }

    def compile(self, query: oql.Query) -> CompiledQuery:
        where = self._expr(query.where) if query.where is not None else None
        terms = [self._order_term(item) for item in query.order_by] or ["i.updated_at DESC"]
        parts = ["SELECT i.id FROM issue i"]
        parts.extend(sql for name, sql in _JOINS.items() if name in self._joins)
        if where is not None:
            parts.append(f"WHERE {where}")
        parts.append("ORDER BY " + ", ".join([*terms, "i.id DESC"]))
        return CompiledQuery(" ".join(parts), self._params)

    def _bind(self, value: object) -> str:
        name = f"p{len(self._params)}"
        self._params[name] = value
        return f":{name}"

    def _bind_all(self, values: Sequence[object]) -> str:
        return "(" + ", ".join(self._bind(value) for value in values) + ")"

    def _expr(self, expr: oql.Expr) -> str:
        if isinstance(expr, oql.Clause):
            return self._clause(expr)
        if isinstance(expr, oql.Not):
            return f"NOT ({self._expr(expr.item)})"
        if isinstance(expr, oql.And):
            return "(" + " AND ".join(self._expr(item) for item in expr.items) + ")"
        if isinstance(expr, oql.Or):
            return "(" + " OR ".join(self._expr(item) for item in expr.items) + ")"
        raise TypeError(f"not an OQL expression: {expr!r}")

    def _clause(self, clause: oql.Clause) -> str:
        field = self._field(clause)
        if clause.op not in field.ops:
            raise oql.OqlSyntaxError(
                f"operator '{clause.op}' is not supported for {field.name}", clause.position
            )
        return self._handlers[field.kind](field, clause)

    @staticmethod
    def _field(clause: oql.Clause) -> _Field:
        lower = clause.field.lower()
        if lower in _UNAVAILABLE:
            raise oql.OqlSyntaxError(
                f"field '{clause.field}' is not available before {_UNAVAILABLE[lower]}", clause.position
            )
        field = _BY_LOWER.get(lower)
        if field is None:
            detail = f"unknown field '{clause.field}'"
            close = difflib.get_close_matches(lower, list(_BY_LOWER), n=1, cutoff=0.6)
            if close:
                detail += f" — did you mean '{_BY_LOWER[close[0]].name}'?"
            raise oql.OqlSyntaxError(detail, clause.position)
        return field

    @staticmethod
    def _unsupported_function(value: oql.Value) -> oql.OqlSyntaxError:
        return oql.OqlSyntaxError(f"function '{value.text}' is not supported here", value.position)

    def _word(self, value: oql.Value) -> str:
        if value.kind == "function":
            raise self._unsupported_function(value)
        return value.text

    def _words(self, clause: oql.Clause, *, upper: bool = False, strip: bool = False) -> list[str]:
        words = [self._word(value) for value in clause.values]
        if strip or upper:
            words = [word.strip() for word in words]
        if upper:
            words = [word.upper() for word in words]
        return words

    @staticmethod
    def _membership(column: str, clause: oql.Clause, placeholders: str) -> str:
        return f"{column} {'NOT IN' if clause.op in _NEGATIVE else 'IN'} {placeholders}"

    @staticmethod
    def _wrap_exists(exists: str, clause: oql.Clause) -> str:
        return f"NOT {exists}" if clause.op in _NEGATIVE else exists

    def _key(self, field: _Field, clause: oql.Clause) -> str:
        if field.name == "project":
            self._joins.add("p")
        return self._membership(field.column, clause, self._bind_all(self._words(clause, upper=True)))

    def _enum_value(self, field: _Field, value: oql.Value) -> str:
        text = self._word(value)
        by_lower = {allowed.lower(): allowed for allowed in field.allowed}
        if text.lower() in by_lower:
            return by_lower[text.lower()]
        close = _suggest(text, list(field.allowed))
        if close is not None:
            raise oql.OqlSyntaxError(f"{field.name} '{text}' — did you mean '{close}'?", value.position)
        raise oql.OqlSyntaxError(
            f"{field.name} '{text}' is not one of: {', '.join(field.allowed)}", value.position
        )

    def _enum(self, field: _Field, clause: oql.Clause) -> str:
        values = [self._enum_value(field, value) for value in clause.values]
        return self._membership(field.column, clause, self._bind_all(values))

    def _category(self, field: _Field, clause: oql.Clause) -> str:
        categories = {self._enum_value(field, value) for value in clause.values}
        statuses = [s for s in issues.STATUSES if issues.STATUS_CATEGORY[s] in categories]
        return self._membership(field.column, clause, self._bind_all(statuses))

    def _person(self, value: oql.Value) -> people.Person:
        if value.kind == "function":
            if value.text.lower() != "me":
                raise self._unsupported_function(value)
            self._no_arguments(value, "me")
        try:
            if value.kind == "function":
                return people.get_me(self._conn)
            return people.lookup_person(self._conn, value.text)
        except NotFoundError as exc:
            detail = str(exc) if value.kind == "function" else f"unknown person '{value.text}'"
            raise oql.OqlSyntaxError(detail, value.position) from None
        except ValidationError as exc:
            raise oql.OqlSyntaxError(str(exc), value.position) from None

    @staticmethod
    def _empty(column: str, clause: oql.Clause) -> str:
        return f"{column} IS NULL" if clause.op == "is empty" else f"{column} IS NOT NULL"

    def _person_clause(self, field: _Field, clause: oql.Clause) -> str:
        if clause.op in _EMPTY:
            return self._empty(field.column, clause)
        ids = self._bind_all([self._person(value).id for value in clause.values])
        if clause.op in _NEGATIVE:
            return f"({field.column} IS NULL OR {field.column} NOT IN {ids})"
        return f"COALESCE({field.column} IN {ids}, 0)"

    def _label(self, field: _Field, clause: oql.Clause) -> str:
        if clause.op in _EMPTY:
            exists = "EXISTS (SELECT 1 FROM issue_label il WHERE il.issue_id = i.id)"
            return exists if clause.op == "is not empty" else f"NOT {exists}"
        names = self._bind_all(self._words(clause, strip=True))
        return self._wrap_exists(
            "EXISTS (SELECT 1 FROM issue_label il JOIN label l ON l.id = il.label_id"
            f" WHERE il.issue_id = i.id AND l.name COLLATE NOCASE IN {names})",
            clause,
        )

    def _component(self, field: _Field, clause: oql.Clause) -> str:
        if clause.op in _EMPTY:
            exists = "EXISTS (SELECT 1 FROM issue_component ic WHERE ic.issue_id = i.id)"
            return exists if clause.op == "is not empty" else f"NOT {exists}"
        names = self._bind_all(self._words(clause, strip=True))
        return self._wrap_exists(
            "EXISTS (SELECT 1 FROM issue_component ic JOIN component c ON c.id = ic.component_id"
            f" WHERE ic.issue_id = i.id AND c.name COLLATE NOCASE IN {names})",
            clause,
        )

    def _parent(self, field: _Field, clause: oql.Clause) -> str:
        if clause.op in _EMPTY:
            return self._empty("i.parent_id", clause)
        keys = self._bind_all(self._words(clause, upper=True))
        parents = f"(SELECT t.id FROM issue t WHERE t.key IN {keys})"
        if clause.op in _NEGATIVE:
            return f"(i.parent_id IS NULL OR i.parent_id NOT IN {parents})"
        return f"COALESCE(i.parent_id IN {parents}, 0)"

    def _issue_link(self, field: _Field, clause: oql.Clause) -> str:
        keys = self._bind_all(self._words(clause, upper=True))[1:-1]
        return _ISSUE_LINK_SQL[field.kind].format(keys=keys)

    def _no_arguments(self, value: oql.Value, name: str) -> None:
        if value.args:
            raise oql.OqlSyntaxError(f"{name}() takes no arguments", value.args[0].position)

    def _offset(self, value: oql.Value, name: str, unit: str) -> int:
        if not value.args:
            return 0
        argument = value.args[0]
        match = _RELATIVE.fullmatch(argument.text) if argument.kind == "relative" else None
        if len(value.args) > 1 or match is None or match.group(3) != unit:
            raise oql.OqlSyntaxError(
                f"invalid offset '{argument.text}' for {name}() (use e.g. -1{unit} or +1{unit})",
                argument.position,
            )
        return int(match.group(2)) * (-1 if match.group(1) == "-" else 1)

    def _date_function(self, value: oql.Value) -> datetime:
        entry = _DATE_FUNCTIONS.get(value.text.lower())
        if entry is None:
            raise self._unsupported_function(value)
        name, unit = entry
        if unit is None:
            self._no_arguments(value, name)
            return self._now
        offset = self._offset(value, name, unit)
        today = self._now.date()
        if unit == "d":
            day = today + timedelta(days=offset)
        else:
            day = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
            if name == "endOfWeek":
                day += timedelta(days=6)
        return datetime.combine(day, _END_OF_DAY if name.startswith("end") else time.min)

    def _resolve(self, value: oql.Value) -> tuple[datetime, bool]:
        """The local (naive) datetime a value stands for and whether it was a plain date literal."""
        invalid = oql.OqlSyntaxError(
            f"invalid date '{value.text}' (use YYYY-MM-DD, -7d or a date function)", value.position
        )
        try:
            if value.kind == "function":
                return self._date_function(value), False
            if value.kind == "relative":
                match = _RELATIVE.fullmatch(value.text)
                assert match is not None
                amount = int(match.group(2)) * (-1 if match.group(1) == "-" else 1)
                unit = match.group(3)
                if unit == "m":
                    return _add_months(self._now, amount), False
                return self._now + timedelta(days=amount * (7 if unit == "w" else 1)), False
            if _DATE_LITERAL.fullmatch(value.text):
                return datetime.combine(date.fromisoformat(value.text), time.min), True
        except (OverflowError, ValueError):
            raise invalid from None
        raise invalid

    def _date(self, field: _Field, clause: oql.Clause) -> str:
        if clause.op in _EMPTY:
            return self._empty(field.column, clause)
        when, _ = self._resolve(clause.values[0])
        day = self._bind(when.date().isoformat())
        return f"COALESCE({field.column} {_SQL_COMPARE[clause.op]} {day}, 0)"

    def _timestamp(self, field: _Field, clause: oql.Clause) -> str:
        if clause.op in _EMPTY:
            return self._empty(field.column, clause)
        when, is_literal = self._resolve(clause.values[0])
        column = field.column
        if clause.op in ("=", "!=") and is_literal:
            start = self._bind(_utc_iso(when))
            end = self._bind(_utc_iso(when + timedelta(days=1)))
            if clause.op == "=":
                return f"COALESCE({column} >= {start} AND {column} < {end}, 0)"
            return f"COALESCE({column} < {start} OR {column} >= {end}, 0)"
        instant = self._bind(_utc_iso(when))
        return f"COALESCE({column} {_SQL_COMPARE[clause.op]} {instant}, 0)"

    def _like(self, field: _Field, clause: oql.Clause) -> str:
        pattern = self._bind(_like_pattern(self._word(clause.values[0])))
        negation = "NOT " if clause.op in _NEGATIVE else ""
        return f"{field.column} {negation}LIKE {pattern} {_ESCAPE}"

    def _comment(self, field: _Field, clause: oql.Clause) -> str:
        pattern = self._bind(_like_pattern(self._word(clause.values[0])))
        return self._wrap_exists(
            "EXISTS (SELECT 1 FROM comment c WHERE c.issue_id = i.id AND c.deleted_at IS NULL"
            f" AND c.body LIKE {pattern} {_ESCAPE})",
            clause,
        )

    def _text(self, field: _Field, clause: oql.Clause) -> str:
        value = clause.values[0]
        try:
            query = fts.to_fts_query(self._word(value))
        except oql.OqlSyntaxError:
            raise
        except ValidationError as exc:
            raise oql.OqlSyntaxError(str(exc), value.position) from None
        subquery = _FTS_PARAMETER.sub(self._bind(query), _FTS_SUBQUERY)
        return f"i.id {'NOT IN' if clause.op in _NEGATIVE else 'IN'} ({subquery})"

    def _source(self, field: _Field, clause: oql.Clause) -> str:
        values = [self._enum_value(field, value) for value in clause.values]
        types = self._bind_all([_SOURCE_TYPES[value] for value in values])
        return self._wrap_exists(_ACTIVE_LINKED_OBJECT.format(condition=f"o.object_type IN {types}"), clause)

    def _source_ref(self, field: _Field, clause: oql.Clause) -> str:
        text = self._word(clause.values[0])
        if clause.op == "~":
            condition = f"o.display_ref LIKE {self._bind(_like_pattern(text))} {_ESCAPE}"
        else:
            condition = f"o.display_ref COLLATE NOCASE = {self._bind(text)}"
        return self._wrap_exists(_ACTIVE_LINKED_OBJECT.format(condition=condition), clause)

    def _order_term(self, item: oql.OrderItem) -> str:
        name = _ORDER_BY_LOWER.get(item.field.lower())
        if name is None:
            detail = f"cannot order by '{item.field}'"
            if item.field.lower() not in _BY_LOWER:
                close = _suggest(item.field, list(_ORDER_TERMS))
                if close is not None:
                    detail += f" — did you mean '{close}'?"
            raise oql.OqlSyntaxError(detail, item.position)
        joins, template = _ORDER_TERMS[name]
        self._joins.update(joins)
        return template.format(d=item.direction.upper())


def compile_query(conn: sqlalchemy.Connection, query: oql.Query | str) -> CompiledQuery:
    """Compile OQL (text or AST) into parameterised SQL that selects issue ids in result order (SRCH-4)."""
    ast = oql.parse(query) if isinstance(query, str) else query
    return _Compiler(conn).compile(ast)


def search_issues(
    conn: sqlalchemy.Connection, query: oql.Query | str, *, limit: int | None = None
) -> list[issues.Issue]:
    """Run an OQL query and return the matching issues in result order."""
    compiled = compile_query(conn, query)
    sql = compiled.sql
    params = dict(compiled.params)
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = limit
    ids = conn.execute(sqlalchemy.text(sql), params).scalars().all()
    return issues.get_issues_by_ids(conn, ids)
