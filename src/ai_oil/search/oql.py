"""OQL, the JQL-like query language: lexer and parser (spec 8.2, SRCH-4 .. SRCH-6)."""

import re
import string
from dataclasses import dataclass

from ai_oil.errors import ValidationError

_KEYWORDS = frozenset({"AND", "OR", "NOT", "IN", "IS", "EMPTY", "ORDER", "BY", "ASC", "DESC"})
_NOT_A_VALUE = frozenset({"AND", "OR", "NOT", "ORDER"})
_WORD_CHARS = frozenset(string.ascii_letters + string.digits + "_.@:/-")
_RELATIVE = re.compile(r"[+-][0-9]+[dwm]", re.IGNORECASE)
_TWO_CHAR_OPS = frozenset({"!=", ">=", "<=", "!~"})
_ONE_CHAR_OPS = frozenset("=<>~")


class OqlSyntaxError(ValidationError):
    """The query text is not valid OQL; `position` is a 0-based character offset into it."""

    def __init__(self, detail: str, position: int) -> None:
        super().__init__(f"{detail} at position {position}")
        self.detail = detail
        self.position = position


def format_error(query: str, exc: OqlSyntaxError) -> str:
    """Three lines for the terminal: the message, the query, and a caret under the offending position."""
    return f"error: {exc.detail} at position {exc.position}\n{query}\n{' ' * exc.position}^"


@dataclass(frozen=True)
class Value:
    """A literal: kind is word, string, relative or function; a function keeps its name in `text`."""

    kind: str
    text: str
    position: int
    args: tuple["Value", ...] = ()


@dataclass(frozen=True)
class Clause:
    """`field op value`, `field [NOT] IN (...)` or `field IS [NOT] EMPTY`; `op` is lower-case."""

    field: str
    op: str
    values: tuple[Value, ...]
    position: int


@dataclass(frozen=True)
class And:
    items: tuple["Expr", ...]


@dataclass(frozen=True)
class Or:
    items: tuple["Expr", ...]


@dataclass(frozen=True)
class Not:
    item: "Expr"


Expr = Clause | And | Or | Not


@dataclass(frozen=True)
class OrderItem:
    field: str
    direction: str
    position: int


@dataclass(frozen=True)
class Query:
    where: Expr | None
    order_by: tuple[OrderItem, ...]


@dataclass(frozen=True)
class _Token:
    kind: str  # word, string, relative, op, lparen, rparen, comma, end
    text: str
    raw: str
    position: int
    call: bool = False  # a word directly followed by "(", i.e. a possible function call


def _read_string(query: str, start: int) -> tuple[_Token, int]:
    chars: list[str] = []
    i = start + 1
    while i < len(query):
        ch = query[i]
        if ch == "\\" and query[i + 1 : i + 2] in ('"', "\\"):
            chars.append(query[i + 1])
            i += 2
        elif ch == '"':
            return _Token("string", "".join(chars), query[start : i + 1], start), i + 1
        else:
            chars.append(ch)
            i += 1
    raise OqlSyntaxError("unterminated string", start)


def _tokenize(query: str) -> list[_Token]:
    tokens: list[_Token] = []
    i = 0
    while i < len(query):
        ch = query[i]
        if ch.isspace():
            i += 1
        elif ch == '"':
            token, i = _read_string(query, i)
            tokens.append(token)
        elif ch in "+-":
            end = i + 1
            while end < len(query) and query[end] in _WORD_CHARS:
                end += 1
            raw = query[i:end]
            if not _RELATIVE.fullmatch(raw):
                raise OqlSyntaxError(f"unexpected character '{ch}'", i)
            tokens.append(_Token("relative", raw.lower(), raw, i))
            i = end
        elif ch in _WORD_CHARS:
            end = i + 1
            while end < len(query) and query[end] in _WORD_CHARS:
                end += 1
            raw = query[i:end]
            tokens.append(_Token("word", raw, raw, i, call=query[end : end + 1] == "("))
            i = end
        elif ch in "(),":
            tokens.append(_Token({"(": "lparen", ")": "rparen", ",": "comma"}[ch], ch, ch, i))
            i += 1
        elif query[i : i + 2] in _TWO_CHAR_OPS:
            raw = query[i : i + 2]
            tokens.append(_Token("op", raw, raw, i))
            i += 2
        elif ch in _ONE_CHAR_OPS:
            tokens.append(_Token("op", ch, ch, i))
            i += 1
        else:
            raise OqlSyntaxError(f"unexpected character '{ch}'", i)
    tokens.append(_Token("end", "", "", len(query)))
    return tokens


class _Parser:
    def __init__(self, query: str) -> None:
        self._tokens = _tokenize(query)
        self._index = 0

    def _peek(self, offset: int = 0) -> _Token:
        return self._tokens[min(self._index + offset, len(self._tokens) - 1)]

    def _advance(self) -> _Token:
        token = self._peek()
        self._index += 1
        return token

    def _is_keyword(self, *names: str, offset: int = 0) -> bool:
        token = self._peek(offset)
        return token.kind == "word" and token.text.upper() in names

    @staticmethod
    def _fail(detail: str, token: _Token) -> OqlSyntaxError:
        return OqlSyntaxError(detail, token.position)

    def parse(self) -> Query:
        where = None
        if self._peek().kind != "end" and not self._is_keyword("ORDER"):
            where = self._or_expr()
        order_by: tuple[OrderItem, ...] = ()
        if self._is_keyword("ORDER"):
            self._advance()
            if not self._is_keyword("BY"):
                raise self._fail("expected BY after ORDER", self._peek())
            self._advance()
            order_by = self._order_items()
        token = self._peek()
        if token.kind != "end":
            raise self._fail(f"unexpected '{token.raw}'", token)
        return Query(where, order_by)

    def _order_items(self) -> tuple[OrderItem, ...]:
        items: list[OrderItem] = []
        while True:
            field = self._field_name()
            direction = "asc"
            if self._is_keyword("ASC", "DESC"):
                direction = self._advance().text.lower()
            items.append(OrderItem(field.text, direction, field.position))
            if self._peek().kind != "comma":
                return tuple(items)
            self._advance()

    def _field_name(self) -> _Token:
        token = self._peek()
        if token.kind == "end":
            raise self._fail("unexpected end of query", token)
        if token.kind != "word" or token.text.upper() in _KEYWORDS:
            raise self._fail("expected a field name", token)
        return self._advance()

    def _or_expr(self) -> Expr:
        items = [self._and_expr()]
        while self._is_keyword("OR"):
            self._advance()
            items.append(self._and_expr())
        return self._combine(Or, items)

    def _and_expr(self) -> Expr:
        items = [self._not_expr()]
        while self._is_keyword("AND"):
            self._advance()
            items.append(self._not_expr())
        return self._combine(And, items)

    @staticmethod
    def _combine(kind: type[And] | type[Or], items: list[Expr]) -> Expr:
        if len(items) == 1:
            return items[0]
        flat: list[Expr] = []
        for item in items:
            if isinstance(item, kind):
                flat.extend(item.items)
            else:
                flat.append(item)
        return kind(tuple(flat))

    def _not_expr(self) -> Expr:
        if self._is_keyword("NOT"):
            self._advance()
            return Not(self._not_expr())
        if self._peek().kind == "lparen":
            self._advance()
            expr = self._or_expr()
            self._expect_rparen()
            return expr
        return self._clause()

    def _expect_rparen(self) -> None:
        token = self._peek()
        if token.kind != "rparen":
            raise self._fail("expected ')'", token)
        self._advance()

    def _clause(self) -> Clause:
        field = self._field_name()
        token = self._peek()
        if token.kind == "op":
            self._advance()
            return Clause(field.text, token.text, (self._value(),), field.position)
        if self._is_keyword("IN"):
            self._advance()
            return Clause(field.text, "in", self._value_list(), field.position)
        if self._is_keyword("NOT") and self._is_keyword("IN", offset=1):
            self._advance()
            self._advance()
            return Clause(field.text, "not in", self._value_list(), field.position)
        if self._is_keyword("IS"):
            self._advance()
            negated = self._is_keyword("NOT")
            if negated:
                self._advance()
            if not self._is_keyword("EMPTY"):
                raise self._fail("expected EMPTY", self._peek())
            self._advance()
            return Clause(field.text, "is not empty" if negated else "is empty", (), field.position)
        raise self._fail(f"expected an operator after '{field.text}'", token)

    def _value_list(self) -> tuple[Value, ...]:
        token = self._peek()
        if token.kind != "lparen":
            raise self._fail("expected '(' after IN", token)
        self._advance()
        values = [self._value()]
        while self._peek().kind == "comma":
            self._advance()
            values.append(self._value())
        self._expect_rparen()
        return tuple(values)

    def _value(self, *, allow_function: bool = True) -> Value:
        token = self._peek()
        if token.kind in ("string", "relative"):
            self._advance()
            return Value(token.kind, token.text, token.position)
        if token.kind == "word" and token.text.upper() not in _NOT_A_VALUE:
            self._advance()
            if allow_function and token.call:
                return self._function(token)
            return Value("word", token.text, token.position)
        raise self._fail("expected a value", token)

    def _function(self, name: _Token) -> Value:
        self._advance()
        args: list[Value] = []
        if self._peek().kind != "rparen":
            args.append(self._value(allow_function=False))
            while self._peek().kind == "comma":
                self._advance()
                args.append(self._value(allow_function=False))
        self._expect_rparen()
        return Value("function", name.text, name.position, tuple(args))


def parse(query: str) -> Query:
    """Parse OQL text into an AST; raises OqlSyntaxError with the offending position (SRCH-4)."""
    return _Parser(query).parse()
