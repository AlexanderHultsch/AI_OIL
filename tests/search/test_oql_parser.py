import dataclasses

import pytest

from ai_oil.errors import ValidationError
from ai_oil.search.oql import (
    And,
    Clause,
    Not,
    OqlSyntaxError,
    Or,
    OrderItem,
    Query,
    Value,
    format_error,
    parse,
)


def w(text):
    return ("word", text)


def s(text):
    return ("string", text)


def r(text):
    return ("relative", text)


def fn(name, *args):
    return ("function", name, tuple(args))


def c(field, op, *values):
    return ("clause", field, op, tuple(values))


def shape_value(value):
    if value.kind == "function":
        return fn(value.text, *(shape_value(arg) for arg in value.args))
    return (value.kind, value.text)


def shape(node):
    """The AST without positions, as plain tuples, so expectations stay readable."""
    if isinstance(node, Query):
        where = None if node.where is None else shape(node.where)
        return where, tuple((item.field, item.direction) for item in node.order_by)
    if isinstance(node, Clause):
        return c(node.field, node.op, *(shape_value(v) for v in node.values))
    if isinstance(node, And):
        return ("and", *(shape(item) for item in node.items))
    if isinstance(node, Or):
        return ("or", *(shape(item) for item in node.items))
    if isinstance(node, Not):
        return ("not", shape(node.item))
    raise AssertionError(node)


def where(query):
    return shape(parse(query))[0]


SPEC_EXAMPLES = [
    (
        "status = Blocked AND assignee = me() ORDER BY priority, due",
        (
            ("and", c("status", "=", w("Blocked")), c("assignee", "=", fn("me"))),
            (("priority", "asc"), ("due", "asc")),
        ),
    ),
    (
        "project = OIL AND statusCategory != Done AND due < startOfDay() ORDER BY due",
        (
            (
                "and",
                c("project", "=", w("OIL")),
                c("statusCategory", "!=", w("Done")),
                c("due", "<", fn("startOfDay")),
            ),
            (("due", "asc"),),
        ),
    ),
    (
        'text ~ "inductor lead time" AND label IN (supplier, b-sample)',
        (
            (
                "and",
                c("text", "~", s("inductor lead time")),
                c("label", "in", w("supplier"), w("b-sample")),
            ),
            (),
        ),
    ),
    (
        "source = email AND sourceUpdated >= -2d",
        (("and", c("source", "=", w("email")), c("sourceUpdated", ">=", r("-2d"))), ()),
    ),
    (
        'priority IN (P1, P2) AND updated <= -5d AND statusCategory = "In Progress"',
        (
            (
                "and",
                c("priority", "in", w("P1"), w("P2")),
                c("updated", "<=", r("-5d")),
                c("statusCategory", "=", s("In Progress")),
            ),
            (),
        ),
    ),
    (
        "parent = OIL-10 OR issueLink = OIL-10",
        (("or", c("parent", "=", w("OIL-10")), c("issueLink", "=", w("OIL-10"))), ()),
    ),
]


@pytest.mark.parametrize(("query", "expected"), SPEC_EXAMPLES)
def test_spec_examples_parse_to_the_expected_ast(query, expected):
    assert shape(parse(query)) == expected


def test_empty_and_blank_queries_have_no_where_and_no_order():
    assert parse("") == Query(None, ())
    assert parse("  \t\n ") == Query(None, ())


def test_precedence_and_binds_tighter_than_or():
    assert where("a = 1 OR b = 2 AND c = 3") == (
        "or",
        c("a", "=", w("1")),
        ("and", c("b", "=", w("2")), c("c", "=", w("3"))),
    )
    assert where("a = 1 AND b = 2 OR c = 3") == (
        "or",
        ("and", c("a", "=", w("1")), c("b", "=", w("2"))),
        c("c", "=", w("3")),
    )


def test_precedence_not_binds_tightest():
    assert where("NOT a = 1 AND b = 2") == ("and", ("not", c("a", "=", w("1"))), c("b", "=", w("2")))
    assert where("NOT NOT a = 1") == ("not", ("not", c("a", "=", w("1"))))


def test_parentheses_override_precedence():
    assert where("(a = 1 OR b = 2) AND c = 3") == (
        "and",
        ("or", c("a", "=", w("1")), c("b", "=", w("2"))),
        c("c", "=", w("3")),
    )
    assert where("NOT (a = 1 OR b = 2)") == ("not", ("or", c("a", "=", w("1")), c("b", "=", w("2"))))
    assert where("((a = 1))") == c("a", "=", w("1"))


def test_same_kind_chains_and_nested_groups_are_flattened():
    a, b, d, e = (c(name, "=", w("1")) for name in "abde")
    cc = c("c", "=", w("1"))
    assert where("a = 1 AND b = 1 AND c = 1") == ("and", a, b, cc)
    assert where("a = 1 OR b = 1 OR c = 1") == ("or", a, b, cc)
    assert where("a = 1 AND (b = 1 AND c = 1)") == ("and", a, b, cc)
    assert where("(a = 1 AND b = 1) AND (c = 1 AND d = 1)") == ("and", a, b, cc, d)
    assert where("a = 1 OR (b = 1 OR c = 1) OR e = 1") == ("or", a, b, cc, e)
    assert where("a = 1 AND (b = 1 OR c = 1)") == ("and", a, ("or", b, cc))


@pytest.mark.parametrize("op", ["=", "!=", ">", ">=", "<", "<=", "~", "!~"])
def test_comparison_operators(op):
    assert where(f"f {op} x") == c("f", op, w("x"))
    assert where(f"f{op}x") == c("f", op, w("x"))


def test_in_not_in_and_is_empty_operators():
    assert where("f IN (a)") == c("f", "in", w("a"))
    assert where("f in (a, b, c)") == c("f", "in", w("a"), w("b"), w("c"))
    assert where("f NOT IN (a, b)") == c("f", "not in", w("a"), w("b"))
    assert where("f not  in(a)") == c("f", "not in", w("a"))
    assert where("f IS EMPTY") == c("f", "is empty")
    assert where("f IS NOT EMPTY") == c("f", "is not empty")
    assert where("f is not empty") == c("f", "is not empty")


def test_in_list_accepts_mixed_value_kinds():
    assert where('f IN (a, "b c", -3d, me())') == c("f", "in", w("a"), s("b c"), r("-3d"), fn("me"))


def test_keywords_are_case_insensitive():
    assert shape(parse("order by priority desc")) == (None, (("priority", "desc"),))
    assert shape(parse("a = 1 and b = 2 Or not c = 3 OrDeR By d Asc")) == (
        ("or", ("and", c("a", "=", w("1")), c("b", "=", w("2"))), ("not", c("c", "=", w("3")))),
        (("d", "asc"),),
    )


def test_field_case_is_preserved_and_op_is_lower_case():
    clause = parse("StatusCategory IN (x)").where
    assert clause.field == "StatusCategory"
    assert clause.op == "in"
    assert parse("a IS NOT EMPTY").where.op == "is not empty"
    assert parse("order by Due DESC").order_by[0].field == "Due"


def test_order_by_without_where():
    assert parse("ORDER BY due") == Query(None, (OrderItem("due", "asc", 9),))
    assert shape(parse("ORDER BY due DESC, priority ASC, key")) == (
        None,
        (("due", "desc"), ("priority", "asc"), ("key", "asc")),
    )


def test_order_direction_defaults_to_asc():
    assert [item.direction for item in parse("a = 1 ORDER BY x, y DESC, z ASC").order_by] == [
        "asc",
        "desc",
        "asc",
    ]


def test_strings_with_escapes():
    assert where(r'a ~ "say \"hi\""') == c("a", "~", s('say "hi"'))
    assert where(r'a ~ "back\\slash"') == c("a", "~", s("back\\slash"))
    assert where(r'a ~ "C:\temp"') == c("a", "~", s("C:\\temp"))
    assert where(r'a ~ "ends with backslash\\"') == c("a", "~", s("ends with backslash\\"))
    assert where('a = ""') == c("a", "=", s(""))
    assert where('a = "AND OR ORDER BY ( ) ,"') == c("a", "=", s("AND OR ORDER BY ( ) ,"))


def test_quoted_keyword_is_a_plain_string_value():
    assert where('a = "AND"') == c("a", "=", s("AND"))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("-7d", r("-7d")),
        ("+2w", r("+2w")),
        ("-1m", r("-1m")),
        ("-12D", r("-12d")),
        ("+3W", r("+3w")),
        ("OIL-10", w("OIL-10")),
        ("2026-11-15", w("2026-11-15")),
        ("P1", w("P1")),
        ("alex@example.com", w("alex@example.com")),
        ("a-7d", w("a-7d")),
        ("7d", w("7d")),
        ("In", w("In")),
        ("Desc", w("Desc")),
        ("empty", w("empty")),
        ("by", w("by")),
        ("NULL", w("NULL")),
        ("null", w("null")),
        ("https://x.y/z:1", w("https://x.y/z:1")),
    ],
)
def test_value_kinds(text, expected):
    assert where(f"f = {text}") == c("f", "=", expected)


def test_relative_date_next_to_punctuation():
    assert where("f IN (-7d,+2w)") == c("f", "in", r("-7d"), r("+2w"))


def test_functions_with_and_without_arguments():
    assert where("a = me()") == c("a", "=", fn("me"))
    assert where("a < startOfDay(-7d)") == c("a", "<", fn("startOfDay", r("-7d")))
    assert where("a < endOfWeek(+1w)") == c("a", "<", fn("endOfWeek", r("+1w")))
    assert where('a = f(x, "y z", -1d)') == c("a", "=", fn("f", w("x"), s("y z"), r("-1d")))
    assert where("a = StartOfDay()") == c("a", "=", fn("StartOfDay"))
    assert where("a IN (me(), bob)") == c("a", "in", fn("me"), w("bob"))


def test_function_name_can_be_a_non_reserved_keyword():
    assert where("a = in()") == c("a", "=", fn("in"))


def test_keyword_directly_followed_by_parenthesis_stays_a_keyword():
    assert where("a = 1 AND(b = 2)") == ("and", c("a", "=", w("1")), c("b", "=", w("2")))
    assert where("a IN(x)") == c("a", "in", w("x"))
    assert where("NOT(a = 1)") == ("not", c("a", "=", w("1")))


def test_positions_are_recorded_for_clauses_values_and_order_items():
    query = 'status = Blocked AND due < startOfDay(-1d) AND text ~ "x y" ORDER BY due DESC, key'
    parsed = parse(query)
    first, second, third = parsed.where.items
    assert first == Clause("status", "=", (Value("word", "Blocked", query.index("Blocked")),), 0)
    assert second.position == query.index("due")
    function = second.values[0]
    assert function == Value(
        "function",
        "startOfDay",
        query.index("startOfDay"),
        (Value("relative", "-1d", query.index("-1d")),),
    )
    assert third.position == query.index("text")
    assert third.values[0] == Value("string", "x y", query.index('"x y"'))
    assert parsed.order_by == (
        OrderItem("due", "desc", query.rindex("due")),
        OrderItem("key", "asc", query.rindex("key")),
    )


def test_ast_nodes_are_frozen():
    clause = parse("a = 1").where
    with pytest.raises(dataclasses.FrozenInstanceError):
        clause.field = "b"


ERRORS = [
    # (query, detail, position)
    ("= 1", "expected a field name", 0),
    ("AND = 1", "expected a field name", 0),
    ("a = 1 AND ORDER BY x", "expected a field name", 10),
    ("a = 1 AND in = 3", "expected a field name", 10),
    ("() ", "expected a field name", 1),
    ('"a" = 1', "expected a field name", 0),
    ('ORDER BY "x"', "expected a field name", 9),
    ("ORDER BY a, ", "unexpected end of query", 12),
    ("a = 1 ORDER BY a,, b", "expected a field name", 17),
    ("status", "expected an operator after 'status'", 6),
    ("Status 5", "expected an operator after 'Status'", 7),
    ("Status NOT = 3", "expected an operator after 'Status'", 7),
    ("a(", "expected an operator after 'a'", 1),
    ("status =", "expected a value", 8),
    ("status = AND", "expected a value", 9),
    ("status = or x", "expected a value", 9),
    ("status = NOT", "expected a value", 9),
    ("status = ORDER BY x", "expected a value", 9),
    ("status = )", "expected a value", 9),
    ("status IN ()", "expected a value", 11),
    ("status IN (a,)", "expected a value", 13),
    ("a = me(,)", "expected a value", 7),
    ("a = me(1,)", "expected a value", 9),
    ("(a = 1", "expected ')'", 6),
    ("(a = 1 b = 2)", "expected ')'", 7),
    ("a IN (1, 2", "expected ')'", 10),
    ("a IN (1 2)", "expected ')'", 8),
    ("a = me(1 2)", "expected ')'", 9),
    ("a = me(", "expected a value", 7),
    ("a = me(b(c))", "expected ')'", 8),
    ("a IN 1", "expected '(' after IN", 5),
    ("a NOT IN 1", "expected '(' after IN", 9),
    ("a IN", "expected '(' after IN", 4),
    ("a IS NULL", "expected EMPTY", 5),
    ("a IS NOT 3", "expected EMPTY", 9),
    ("a IS NOT", "expected EMPTY", 8),
    ("a IS", "expected EMPTY", 4),
    ("ORDER x", "expected BY after ORDER", 6),
    ("a = 1 ORDER", "expected BY after ORDER", 11),
    ("a = 1 b", "unexpected 'b'", 6),
    ("a = 1 )", "unexpected ')'", 6),
    ('a = 1 "x y"', "unexpected '\"x y\"'", 6),
    ("a = 1 -3d", "unexpected '-3d'", 6),
    ("a = me ()", "unexpected '('", 7),
    ("a = 1 ORDER BY b c", "unexpected 'c'", 17),
    ("a = 1 ORDER BY b DESC ASC", "unexpected 'ASC'", 22),
    ("a = 1 ORDER BY b ORDER BY c", "unexpected 'ORDER'", 17),
    ("a = 1 = 2", "unexpected '='", 6),
    ("a = 1 AND", "unexpected end of query", 9),
    ("a = 1 OR ", "unexpected end of query", 9),
    ("NOT", "unexpected end of query", 3),
    ("ORDER BY", "unexpected end of query", 8),
    ("(", "unexpected end of query", 1),
    ("a = 1 AND (", "unexpected end of query", 11),
    ('a = "abc', "unterminated string", 4),
    ('a = "abc\\"', "unterminated string", 4),
    ('a ~ "x" AND b ~ "y', "unterminated string", 16),
    ("a = #", "unexpected character '#'", 4),
    ("a = 1 ; b = 2", "unexpected character ';'", 6),
    ("a ! b", "unexpected character '!'", 2),
    ("a = -x", "unexpected character '-'", 4),
    ("a = -", "unexpected character '-'", 4),
    ("a = +", "unexpected character '+'", 4),
    ("a = +7d9", "unexpected character '+'", 4),
    ("a = 5 +x", "unexpected character '+'", 6),
    ("a = é", "unexpected character 'é'", 4),
    ("a = {1}", "unexpected character '{'", 4),
]


@pytest.mark.parametrize(("query", "detail", "position"), ERRORS)
def test_syntax_errors_carry_message_and_position(query, detail, position):
    with pytest.raises(OqlSyntaxError) as info:
        parse(query)
    assert (info.value.detail, info.value.position) == (detail, position)
    assert str(info.value) == f"{detail} at position {position}"


def test_syntax_error_is_a_validation_error():
    with pytest.raises(ValidationError):
        parse("a =")


def test_null_is_not_a_keyword():
    assert where("assignee = null") == c("assignee", "=", w("null"))
    with pytest.raises(OqlSyntaxError) as info:
        parse("assignee IS NULL")
    assert info.value.detail == "expected EMPTY"


def test_format_error_marks_the_position_with_a_caret():
    query = "status = Blocked AND AND x = 1"
    with pytest.raises(OqlSyntaxError) as info:
        parse(query)
    lines = format_error(query, info.value).split("\n")
    assert lines == [
        "error: expected a field name at position 21",
        query,
        " " * 21 + "^",
    ]


def test_format_error_caret_at_start_and_end():
    with pytest.raises(OqlSyntaxError) as info:
        parse("= 1")
    assert format_error("= 1", info.value) == "error: expected a field name at position 0\n= 1\n^"
    with pytest.raises(OqlSyntaxError) as info:
        parse("status =")
    assert format_error("status =", info.value) == (
        "error: expected a value at position 8\nstatus =\n        ^"
    )


def test_format_error_for_an_error_built_by_hand():
    exc = OqlSyntaxError("boom", 2)
    assert (exc.detail, exc.position, str(exc)) == ("boom", 2, "boom at position 2")
    assert format_error("abcd", exc) == "error: boom at position 2\nabcd\n  ^"
