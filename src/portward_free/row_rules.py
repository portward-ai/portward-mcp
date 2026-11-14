"""Row rules (#132, Personal): a profile can limit a table it grants to the rows matching fixed values, such as
`region IN ('EU', 'US') AND amount >= 100`.

A rule is never SQL. The user's text is parsed by sqlglot and accepted only when it is made of `column IN (values)`,
comparisons of a column with one value (=, <>, <, <=, >, >=) and AND / OR (with brackets); values are text or
numbers. It is kept as a small tree (`Cond`), stored and synced as JSON of that tree, checked again on arrival
(`from_stored`), and rendered back into each source's SQL by building sqlglot nodes (`render`), so nothing the user
or another computer wrote ever reaches a database as text. A rule naming a column its table doesn't have matches no
row (that comparison renders as false; with no NOT in the grammar, that only ever narrows).

How rules combine with levels is in grants.py (`Grants.cells`) and the rewrite in row_cells.py.

A standing filter (#133, "always leave out test accounts") is a RowRule with `standing` set: the same grammar, tree
and storage, but it says which rows *every* read of its table keeps, whichever applied profile reaches the table, so
it only ever narrows (grants.Grants.applied joins it into every profile's reach with AND).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

MAX_TEXT = 2000  # characters of rule text
MAX_VALUES = 500  # values in one IN list
NAME = re.compile(r"[^.\s*][^.*]*")  # a column or table name: no dots, no wildcard (as profiles.NAME_PART)
OPS = {exp.EQ: "=", exp.NEQ: "<>", exp.LT: "<", exp.LTE: "<=", exp.GT: ">", exp.GTE: ">="}
FLIPPED = {"=": "=", "<>": "<>", "<": ">", "<=": ">=", ">": "<", ">=": "<="}
NODES = {v: k for k, v in OPS.items()}
GRAMMAR = ("a row rule is made of column IN (values), comparisons of a column with one value (=, <>, <, <=, >, >=) "
           "and AND / OR, with text in single quotes and numbers as they are")

Value = str | int | float


class RowRuleError(ValueError):
    """A row rule that isn't in the grammar; the message is safe to show to the user."""


@dataclass(frozen=True)
class Compare:
    column: str
    op: str
    value: Value


@dataclass(frozen=True)
class InList:
    column: str
    values: tuple[Value, ...]


@dataclass(frozen=True)
class AllOf:
    parts: tuple[Cond, ...]


@dataclass(frozen=True)
class AnyOf:
    parts: tuple[Cond, ...]


Cond = Compare | InList | AllOf | AnyOf


@dataclass(frozen=True)
class RowRule:
    """The rows of `table` (in `schema`, or the connection's default when None) one profile reaches; with `standing`,
    the rows every read of the table keeps (a standing filter)."""

    table: str
    where: Cond
    schema: str | None = None
    standing: bool = False

    @classmethod
    def parse(cls, table: str, text: str, schema: str | None = None, *, standing: bool = False) -> RowRule:
        return cls(_name(table, "table"), parse(text), _name(schema, "schema") if schema else None, standing)

    @property
    def text(self) -> str:
        """The rule as Portward shows it, re-rendered from the tree."""
        return render(self.where).sql()

    @property
    def columns(self) -> frozenset[str]:
        """The columns it names, lower case."""
        return frozenset(c.lower() for c in columns_of(self.where))

    def about(self, table_name: str, schema: str | None) -> bool:
        """Whether it is the rule for that table (names compared without case; a rule without schema matches any)."""
        return self.table.lower() == table_name.lower() and (
            self.schema is None or (schema or "").lower() == self.schema.lower())

    def stored(self) -> dict[str, Any]:
        found: dict[str, Any] = {"table": self.table, "where": _stored(self.where)}
        if self.schema:
            found["schema"] = self.schema
        if self.standing:
            found["standing"] = True
        return found

    @classmethod
    def from_stored(cls, data: Any) -> RowRule:
        """A rule back from its stored (or synced) form, checked against the grammar again."""
        if not isinstance(data, dict):
            raise RowRuleError("a row rule must be an object")
        schema = data.get("schema")
        if schema is not None and not isinstance(schema, str):
            raise RowRuleError("a row rule's schema must be text")
        standing = data.get("standing", False)
        if not isinstance(standing, bool):
            raise RowRuleError("a row rule's standing mark must be true or false")
        return cls(_name(data.get("table"), "table"), _from_stored(data.get("where"), 0),
                   _name(schema, "schema") if schema else None, standing)


def _name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value.strip()):
        raise RowRuleError(f"{value!r} isn't a {what} name")
    return value.strip()


# --- parsing the user's text ----------------------------------------------------------------------------------------

def parse(text: str) -> Cond:
    """The rule in `text`, or RowRuleError when anything in it is outside the grammar."""
    text = str(text).strip()
    if not text:
        raise RowRuleError("write the rows to keep, for example: region IN ('EU', 'US')")
    if len(text) > MAX_TEXT:
        raise RowRuleError(f"keep a row rule under {MAX_TEXT} characters")
    try:
        statements = [s for s in sqlglot.parse(text) if s is not None]
    except (SqlglotError, ValueError):
        raise RowRuleError(f"{text!r} can't be read: {GRAMMAR}") from None
    if len(statements) != 1:
        raise RowRuleError(f"one rule only: {GRAMMAR}")
    return _cond(statements[0])


def _cond(node: exp.Expression) -> Cond:
    if isinstance(node, exp.Paren):
        return _cond(node.this)
    if isinstance(node, exp.And):
        return AllOf(tuple(_flat(node, exp.And)))
    if isinstance(node, exp.Or):
        return AnyOf(tuple(_flat(node, exp.Or)))
    if type(node) in OPS:
        op = OPS[type(node)]  # type: ignore[index]
        left, right = node.this, node.expression
        if isinstance(right, exp.Column):
            left, right, op = right, left, FLIPPED[op]
        return Compare(_column(left), op, _value(right))
    if isinstance(node, exp.In):
        if node.args.get("query") or node.args.get("unnest") or node.args.get("field"):
            raise RowRuleError(f"IN takes a list of values, not a query: {GRAMMAR}")
        values = tuple(_value(v) for v in node.expressions)
        if not values or len(values) > MAX_VALUES:
            raise RowRuleError(f"IN takes 1 to {MAX_VALUES} values")
        return InList(_column(node.this), values)
    raise RowRuleError(f"{node.sql()!r} isn't allowed: {GRAMMAR}")


def _flat(node: exp.Expression, kind: type) -> Iterable[Cond]:
    for side in (node.this, node.expression):
        inner = side.this if isinstance(side, exp.Paren) and isinstance(side.this, kind) else side
        if isinstance(inner, kind):
            yield from _flat(inner, kind)
        else:
            yield _cond(side)


def _column(node: exp.Expression) -> str:
    if not isinstance(node, exp.Column) or not isinstance(node.this, exp.Identifier) or node.table:
        raise RowRuleError(f"{node.sql()!r} isn't a column of the table (name it alone, without a table): {GRAMMAR}")
    return _name(node.name, "column")


def _value(node: exp.Expression) -> Value:
    negative = isinstance(node, exp.Neg)
    literal = node.this if negative else node
    if not isinstance(literal, exp.Literal) or (negative and literal.is_string):
        raise RowRuleError(f"{node.sql()!r} isn't a value: {GRAMMAR}")
    if literal.is_string:
        return literal.this
    number = _number(literal.this)
    return -number if negative else number


def _number(text: str) -> int | float:
    try:
        return int(text)
    except ValueError:
        try:
            found = float(text)
        except ValueError:
            raise RowRuleError(f"{text!r} isn't a number") from None
        if found != found or found in (float("inf"), float("-inf")):
            raise RowRuleError(f"{text!r} isn't a number")
        return found


# --- stored form ----------------------------------------------------------------------------------------------------

def _stored(cond: Cond) -> dict[str, Any]:
    if isinstance(cond, Compare):
        return {"op": cond.op, "column": cond.column, "value": cond.value}
    if isinstance(cond, InList):
        return {"op": "in", "column": cond.column, "values": list(cond.values)}
    return {"op": "and" if isinstance(cond, AllOf) else "or", "parts": [_stored(p) for p in cond.parts]}


def _stored_value(value: Any) -> Value:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise RowRuleError("a row rule's values are text or numbers")
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise RowRuleError("a row rule's values are text or numbers")
    return value


def _from_stored(data: Any, depth: int) -> Cond:
    if not isinstance(data, dict) or depth > 50:
        raise RowRuleError("a stored row rule isn't in the grammar")
    op = data.get("op")
    if op in NODES:
        return Compare(_name(data.get("column"), "column"), op, _stored_value(data.get("value")))
    if op == "in":
        values = data.get("values")
        if not isinstance(values, list) or not values or len(values) > MAX_VALUES:
            raise RowRuleError(f"IN takes 1 to {MAX_VALUES} values")
        return InList(_name(data.get("column"), "column"), tuple(map(_stored_value, values)))
    if op in ("and", "or"):
        parts = data.get("parts")
        if not isinstance(parts, list) or len(parts) < 2:
            raise RowRuleError("AND / OR join two or more parts")
        found = tuple(_from_stored(p, depth + 1) for p in parts)
        return AllOf(found) if op == "and" else AnyOf(found)
    raise RowRuleError("a stored row rule isn't in the grammar")


# --- rendering ------------------------------------------------------------------------------------------------------

def columns_of(cond: Cond) -> tuple[str, ...]:
    if isinstance(cond, (Compare, InList)):
        return (cond.column,)
    return tuple(c for p in cond.parts for c in columns_of(p))


def _literal(value: Value) -> exp.Expression:
    if isinstance(value, str):
        return exp.Literal.string(value)
    if value < 0:
        return exp.Neg(this=exp.Literal.number(repr(-value)))
    return exp.Literal.number(repr(value))


NEVER = exp.EQ(this=exp.Literal.number(1), expression=exp.Literal.number(0))  # matches no row, on every engine


def render(cond: Cond, real: Mapping[str, str] | None = None, qualifier: str | None = None) -> exp.Expression:
    """`cond` as sqlglot nodes. With `real` (lower-case name -> the table's own name), columns are quoted as the
    table names them, qualified by `qualifier` when given, and a comparison naming a column the table doesn't have
    becomes NEVER. Without, plain names (for showing the rule)."""
    if isinstance(cond, (AllOf, AnyOf)):
        parts = [render(p, real, qualifier) for p in cond.parts]
        return exp.and_(*parts) if isinstance(cond, AllOf) else exp.or_(*parts)
    if real is None:
        column: exp.Expression = exp.column(exp.to_identifier(cond.column))
    elif cond.column.lower() not in real:
        return NEVER.copy()
    else:
        column = exp.column(exp.to_identifier(real[cond.column.lower()], quoted=True),
                            table=exp.to_identifier(qualifier, quoted=True) if qualifier else None)
    if isinstance(cond, InList):
        return exp.In(this=column, expressions=[_literal(v) for v in cond.values])
    return NODES[cond.op](this=column, expression=_literal(cond.value))
