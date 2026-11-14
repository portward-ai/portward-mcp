"""A metric (#129, Personal): a named definition written as a calculation instead of a sentence. It says what it adds
up (`measure`: one SUM, AVG, COUNT, MIN or MAX over a column of `table`), the filters it always applies, the time
column and the grains it may be cut by, and the columns it may be grouped by. It is part of a profile's context
(context.Context.metrics), approved by the user like every other item, versioned on its own and synced with the
profile. Portward writes the SQL from it (metrics.compiled), so the same question gives the same number.

Everything here is checked when a metric is drafted: the measure and each filter are parsed with sqlglot and kept
only if they are made of plain column names, literal values and a short list of operators, so a metric can never
carry a subquery, a function call or another table into the SQL Portward writes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

GRAINS = ("day", "week", "month", "quarter", "year")
MAX_NAME, MAX_TEXT, MAX_FILTERS, MAX_GROUP_BY = 80, 200, 10, 20
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
MEASURES = (exp.Sum, exp.Avg, exp.Count, exp.Min, exp.Max)
FILTER_NODES = (exp.And, exp.Or, exp.Not, exp.Paren, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In,
                exp.Is, exp.Null, exp.Boolean, exp.Literal, exp.Column, exp.Identifier, exp.Neg, exp.Between,
                exp.Like, exp.ILike)


class MetricError(ValueError):
    """A metric that can't be drafted or run as written; the message is safe to show."""


def _parsed(sql: str, what: str) -> exp.Expression:
    try:
        return sqlglot.parse_one(sql)
    except (SqlglotError, ValueError):
        raise MetricError(f"{what} {sql!r} isn't SQL Portward can read") from None


def _column_name(node: exp.Column, what: str) -> str:
    if node.table or not isinstance(node.this, exp.Identifier) or not NAME.fullmatch(node.name):
        raise MetricError(f"{what}: name columns by their plain name, without a table")
    return node.name


def checked_measure(sql: str) -> str:
    """The measure as canonical SQL ("SUM(amount)", "COUNT(DISTINCT customer_id)", "COUNT(*)")."""
    tree = _parsed(str(sql or "").strip(), "the measure")
    if not isinstance(tree, MEASURES):
        raise MetricError("the measure must be one SUM, AVG, COUNT, MIN or MAX, like SUM(amount)")
    inner = tree.this
    if isinstance(inner, exp.Distinct) and isinstance(tree, exp.Count) and len(inner.expressions) == 1:
        inner = inner.expressions[0]
    if isinstance(inner, exp.Star) and isinstance(tree, exp.Count):
        return tree.sql()
    if not isinstance(inner, exp.Column):
        raise MetricError("the measure adds up one column, like SUM(amount) or COUNT(DISTINCT customer_id)")
    _column_name(inner, "the measure")
    return tree.sql()


def checked_filter(sql: str) -> str:
    """One filter as canonical SQL: column names, literal values, comparisons, IN, BETWEEN, LIKE, IS NULL, AND, OR,
    NOT; nothing else."""
    tree = _parsed(str(sql or "").strip(), "the filter")
    for node in tree.walk():
        if not isinstance(node, FILTER_NODES) or (isinstance(node, exp.In) and node.args.get("query")):
            raise MetricError(f"the filter {sql!r} may only compare columns with values (=, <>, <, >, IN, BETWEEN, "
                              "LIKE, IS NULL, AND, OR, NOT)")
        if isinstance(node, exp.Column):
            _column_name(node, f"the filter {sql!r}")
    if not any(isinstance(n, exp.Column) for n in tree.walk()):
        raise MetricError(f"the filter {sql!r} doesn't name a column")
    return tree.sql()


def _name(value: str, what: str) -> str:
    cleaned = str(value or "").strip()
    if not NAME.fullmatch(cleaned):
        raise MetricError(f"{what} {value!r}: use a plain table or column name")
    return cleaned


def columns_in(sql: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(c.name for c in sqlglot.parse_one(sql).find_all(exp.Column)))


@dataclass(frozen=True)
class Metric:
    name: str
    measure: str
    table: str
    filters: tuple[str, ...] = ()
    time: str | None = None
    grains: tuple[str, ...] = ()
    group_by: tuple[str, ...] = ()
    text: str = ""  # the one-liner the AI gets with the schema
    version: int = 1

    @property
    def key(self) -> str:
        return self.name.lower()

    @property
    def about(self) -> str:
        return self.name

    @property
    def exact_columns(self) -> tuple[str, ...]:
        """The columns whose real values the metric needs: its filters' and its time column."""
        found = [c for f in self.filters for c in columns_in(f)]
        return tuple(dict.fromkeys([*found, *([self.time] if self.time else [])]))

    @property
    def columns(self) -> tuple[str, ...]:
        """Every column the metric names."""
        return tuple(dict.fromkeys([*columns_in(self.measure), *self.exact_columns, *self.group_by]))

    def same_calculation(self, other: Metric) -> bool:
        return replace(self, version=0) == replace(other, version=0)

    @property
    def summary(self) -> str:
        """The whole calculation in one line, for the user's approval and for picking between two profiles."""
        said = f"{self.name} = {self.measure} from {self.table}"
        if self.filters:
            said += " where " + " and ".join(self.filters)
        if self.time:
            said += f"; by {'/'.join(self.grains)} of {self.time}"
        if self.group_by:
            said += f"; may be grouped by {', '.join(self.group_by)}"
        return said + (f" ({self.text})" if self.text else "")

    def brief(self) -> dict[str, Any]:
        """What describe_source sends: the name and its one-liner."""
        return {"name": self.name, "text": self.text}

    def payload(self) -> dict[str, Any]:
        return {"name": self.name, "text": self.text, "measure": self.measure, "table": self.table,
                "filters": list(self.filters), "time_column": self.time, "grains": list(self.grains),
                "group_by": list(self.group_by), "version": self.version}

    def stored(self) -> dict[str, Any]:
        return {"name": self.name, "measure": self.measure, "table": self.table, "filters": list(self.filters),
                "time": self.time, "grains": list(self.grains), "group_by": list(self.group_by), "text": self.text,
                "version": self.version}

    @classmethod
    def from_stored(cls, data: dict) -> Metric:
        return cls(data["name"], data["measure"], data["table"], tuple(data.get("filters", ())), data.get("time"),
                   tuple(data.get("grains", ())), tuple(data.get("group_by", ())), data.get("text", ""),
                   int(data.get("version", 1)))


def new_metric(name: str, measure: str, table: str, filters: Iterable[str] = (), time: str | None = None,
               grains: Sequence[str] = (), group_by: Iterable[str] = (), text: str | None = None) -> Metric:
    cleaned = " ".join(str(name or "").split())
    if not cleaned or len(cleaned) > MAX_NAME or '"' in cleaned:
        raise MetricError(f"name the metric in 1 to {MAX_NAME} characters, without double quotes")
    filters = tuple(dict.fromkeys(checked_filter(f) for f in filters))
    if len(filters) > MAX_FILTERS:
        raise MetricError(f"a metric has at most {MAX_FILTERS} filters")
    time = _name(time, "the time column") if time and str(time).strip() else None
    wanted = tuple(dict.fromkeys(str(g).strip().lower() for g in grains))
    if wanted and not time:
        raise MetricError("grains need a time column")
    if unknown := [g for g in wanted if g not in GRAINS]:
        raise MetricError(f"grain {unknown[0]!r} isn't one of {', '.join(GRAINS)}")
    grains = tuple(g for g in GRAINS if g in wanted) if wanted else (GRAINS if time else ())
    group_by = tuple(dict.fromkeys(_name(g, "the group-by column") for g in group_by))
    if len(group_by) > MAX_GROUP_BY:
        raise MetricError(f"a metric allows at most {MAX_GROUP_BY} group-by columns")
    words = " ".join(str(text or "").split())
    if len(words) > MAX_TEXT:
        raise MetricError(f"keep the description to one line ({MAX_TEXT} characters at most)")
    return Metric(cleaned, checked_measure(measure), _name(table, "the table"), filters, time, grains, group_by,
                  words)
