import pytest

from ai_oil.search.oql import (
    And,
    Clause,
    Not,
    Or,
    OrderItem,
    Query,
    Value,
    parse,
    to_text,
    with_order,
)


def shape_value(value):
    if value.kind == "function":
        return ("function", value.text, tuple(shape_value(arg) for arg in value.args))
    return (value.kind, value.text)


def shape(node):
    """The AST without positions, as plain tuples."""
    if isinstance(node, Query):
        where = None if node.where is None else shape(node.where)
        return where, tuple((item.field, item.direction) for item in node.order_by)
    if isinstance(node, Clause):
        return ("clause", node.field, node.op, tuple(shape_value(v) for v in node.values))
    if isinstance(node, And):
        return ("and", *(shape(item) for item in node.items))
    if isinstance(node, Or):
        return ("or", *(shape(item) for item in node.items))
    if isinstance(node, Not):
        return ("not", shape(node.item))
    raise AssertionError(node)


# Every query of tests/search/test_oql_parser.py that parses successfully.
PARSER_QUERIES = [
    "status = Blocked AND assignee = me() ORDER BY priority, due",
    "project = OIL AND statusCategory != Done AND due < startOfDay() ORDER BY due",
    'text ~ "inductor lead time" AND label IN (supplier, b-sample)',
    "source = email AND sourceUpdated >= -2d",
    'priority IN (P1, P2) AND updated <= -5d AND statusCategory = "In Progress"',
    "parent = OIL-10 OR issueLink = OIL-10",
    "",
    "  \t\n ",
    "a = 1 OR b = 2 AND c = 3",
    "a = 1 AND b = 2 OR c = 3",
    "NOT a = 1 AND b = 2",
    "NOT NOT a = 1",
    "(a = 1 OR b = 2) AND c = 3",
    "NOT (a = 1 OR b = 2)",
    "((a = 1))",
    "a = 1 AND b = 1 AND c = 1",
    "a = 1 OR b = 1 OR c = 1",
    "a = 1 AND (b = 1 AND c = 1)",
    "(a = 1 AND b = 1) AND (c = 1 AND d = 1)",
    "a = 1 OR (b = 1 OR c = 1) OR e = 1",
    "a = 1 AND (b = 1 OR c = 1)",
    *(f"f {op} x" for op in ("=", "!=", ">", ">=", "<", "<=", "~", "!~")),
    *(f"f{op}x" for op in ("=", "!=", ">", ">=", "<", "<=", "~", "!~")),
    "f IN (a)",
    "f in (a, b, c)",
    "f NOT IN (a, b)",
    "f not  in(a)",
    "f IS EMPTY",
    "f IS NOT EMPTY",
    "f is not empty",
    'f IN (a, "b c", -3d, me())',
    "order by priority desc",
    "a = 1 and b = 2 Or not c = 3 OrDeR By d Asc",
    "StatusCategory IN (x)",
    "a IS NOT EMPTY",
    "order by Due DESC",
    "ORDER BY due",
    "ORDER BY due DESC, priority ASC, key",
    "a = 1 ORDER BY x, y DESC, z ASC",
    r'a ~ "say \"hi\""',
    r'a ~ "back\\slash"',
    r'a ~ "C:\temp"',
    r'a ~ "ends with backslash\\"',
    'a = ""',
    'a = "AND OR ORDER BY ( ) ,"',
    'a = "AND"',
    "f = -7d",
    "f = +2w",
    "f = -1m",
    "f = -12D",
    "f = +3W",
    "f = OIL-10",
    "f = 2026-11-15",
    "f = P1",
    "f = alex@example.com",
    "f = a-7d",
    "f = 7d",
    "f = In",
    "f = Desc",
    "f = empty",
    "f = by",
    "f = NULL",
    "f = null",
    "f = https://x.y/z:1",
    "f IN (-7d,+2w)",
    "a = me()",
    "a < startOfDay(-7d)",
    "a < endOfWeek(+1w)",
    'a = f(x, "y z", -1d)',
    "a = StartOfDay()",
    "a IN (me(), bob)",
    "a = in()",
    "a = 1 AND(b = 2)",
    "a IN(x)",
    "NOT(a = 1)",
    'status = Blocked AND due < startOfDay(-1d) AND text ~ "x y" ORDER BY due DESC, key',
    "assignee = null",
    "(" * 64 + "a = 1" + ")" * 64,
    "NOT " * 64 + "a = 1",
]


@pytest.mark.parametrize("query", PARSER_QUERIES)
def test_round_trip_keeps_the_ast(query):
    parsed = parse(query)

    assert shape(parse(to_text(parsed))) == shape(parsed)


@pytest.mark.parametrize("query", PARSER_QUERIES)
def test_serialising_is_idempotent(query):
    once = to_text(parse(query))

    assert to_text(parse(once)) == once


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", ""),
        ("status=Blocked", "status = Blocked"),
        ("a = 1 AND (b = 1 OR c = 1)", "a = 1 AND (b = 1 OR c = 1)"),
        ("(a = 1 AND b = 1) OR c = 1", "a = 1 AND b = 1 OR c = 1"),
        ("a = 1 or b = 1 and c = 1", "a = 1 OR b = 1 AND c = 1"),
        ("NOT (a = 1 OR b = 1)", "NOT (a = 1 OR b = 1)"),
        ("NOT (a = 1 AND b = 1)", "NOT (a = 1 AND b = 1)"),
        ("NOT a = 1 AND b = 1", "NOT a = 1 AND b = 1"),
        ("NOT NOT a = 1", "NOT NOT a = 1"),
        ('summary ~ "two words"', 'summary ~ "two words"'),
        ('status = "Blocked"', 'status = "Blocked"'),
        ('a = "AND"', 'a = "AND"'),
        ('a = "order"', 'a = "order"'),
        ("a = in", "a = in"),
        ('a = ""', 'a = ""'),
        (r'a ~ "say \"hi\" \\ now"', r'a ~ "say \"hi\" \\ now"'),
        ('a = "x(y)"', 'a = "x(y)"'),
        ('a = "-3d"', 'a = "-3d"'),
        ("a = -3D", "a = -3d"),
        ("a = me( )", "a = me()"),
        ('a < f( x ,"y z",-1d )', 'a < f(x, "y z", -1d)'),
        ("a < startOfDay(-7d)", "a < startOfDay(-7d)"),
        ("f in (a,b)", "f IN (a, b)"),
        ('f not in (a,"b c")', 'f NOT IN (a, "b c")'),
        ("f is empty", "f IS EMPTY"),
        ("f is not empty", "f IS NOT EMPTY"),
        ("order by due asc", "ORDER BY due"),
        ("a = 1 order by due desc, key asc, x", "a = 1 ORDER BY due DESC, key, x"),
    ],
)
def test_canonical_output(query, expected):
    assert to_text(parse(query)) == expected


def test_a_word_that_cannot_be_bare_is_quoted():
    clause = Clause("a", "=", (Value("word", "has space", 0),), 0)
    assert to_text(Query(clause, ())) == 'a = "has space"'
    for text in ("AND", "or", "Not", "order", "-x", "+1", "", "a,b", "(x)", "é"):
        clause = Clause("a", "=", (Value("word", text, 0),), 0)
        assert to_text(Query(clause, ())).startswith('a = "'), text
    for text in ("IN", "by", "OIL-1", "a.b", "x@y.z", "p:1"):
        clause = Clause("a", "=", (Value("word", text, 0),), 0)
        assert to_text(Query(clause, ())) == f"a = {text}", text


def test_nested_operators_from_a_hand_built_ast():
    a, b, c = (Clause(name, "=", (Value("word", "1", 0),), 0) for name in "abc")

    assert to_text(Query(And((Or((a, b)), c)), ())) == "(a = 1 OR b = 1) AND c = 1"
    assert to_text(Query(Or((And((a, b)), c)), ())) == "a = 1 AND b = 1 OR c = 1"
    assert to_text(Query(Not(Not(a)), ())) == "NOT NOT a = 1"
    assert to_text(Query(Not(And((a, b))), ())) == "NOT (a = 1 AND b = 1)"


def test_with_order_replaces_the_ordering_and_keeps_the_filter():
    query = parse("status = Blocked AND assignee = me() ORDER BY priority, due DESC")

    ordered = with_order(query, "updated", "desc")

    assert ordered.where is query.where
    assert ordered.order_by == (OrderItem("updated", "desc", 0),)
    assert to_text(ordered) == "status = Blocked AND assignee = me() ORDER BY updated DESC"
    assert query.order_by[0].field == "priority"  # the original is untouched


def test_with_order_on_a_query_without_filter_or_ordering():
    assert to_text(with_order(parse(""), "key", "asc")) == "ORDER BY key"
    assert to_text(with_order(parse("a = 1"), "key", "asc")) == "a = 1 ORDER BY key"
