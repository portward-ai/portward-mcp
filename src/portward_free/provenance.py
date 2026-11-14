"""Where a value in a result came from: the source tables and columns behind one result column, and the filters
the query applied, read from the pull's SQL with sqlglot (no data is read).

`trace` is pure. The schema (table -> column names) lets `SELECT *` and unqualified columns in joins resolve; without
it the trace still works for qualified or single-table queries.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError
from sqlglot.lineage import lineage


@dataclass(frozen=True)
class Origin:
    table: str
    column: str | None  # None when the value counts rows (COUNT(*)) rather than reading a column


@dataclass(frozen=True)
class Filter:
    clause: str  # "where", "join", "having" or "limit"
    sql: str


@dataclass(frozen=True)
class Provenance:
    column: str
    kind: str  # "column" (a value read as it is), "aggregate" (SUM, COUNT, ...) or "calculated"
    expression: str  # how the query wrote the column, with table names in place of aliases
    origins: tuple[Origin, ...]
    filters: tuple[Filter, ...]
    grouped_by: tuple[str, ...]  # result columns that identify the group an aggregate value is for
    traced: bool  # False when the column couldn't be followed; origins are then the query's tables

    def payload(self, row: Sequence[Any] | None = None, columns: Sequence[str] = ()) -> dict[str, Any]:
        group = [{"column": c, "value": row[columns.index(c)]} for c in self.grouped_by
                 if row is not None and c in columns]
        return {"column": self.column, "kind": self.kind, "expression": self.expression,
                "from": [{"table": o.table, "column": o.column} for o in self.origins],
                "filters": [{"clause": f.clause, "sql": f.sql} for f in self.filters],
                "group": group, "traced": self.traced}


Schema = Mapping[str, Sequence[str]]


def trace(sql: str, dialect: str, column: str, schema: Schema | None = None) -> Provenance:
    """The provenance of result column `column` of `sql`. Raises ValueError when `sql` has no such column."""
    tree = parse_one(sql, read=dialect)
    names = _real_names(tree)
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if select is None:
        raise ValueError("this query has no SELECT to trace")
    projection = _projection(select, column)
    shown = _with_real_names(projection.unalias() if projection is not None else exp.column(column), names, dialect)
    kind = _kind(projection)
    filters = tuple(_filters(tree, names, dialect))
    grouped_by = _grouped_by(select, kind)
    mapping = {t: {c: "TEXT" for c in cols} for t, cols in (schema or {}).items()}
    try:
        node = lineage(column, tree, mapping or None, dialect=dialect)
        origins = _origins(node)
        traced = True
    except SqlglotError:
        origins, traced = (), False
    if not origins:  # COUNT(*), a constant, or a column sqlglot couldn't follow: name the query's tables
        origins = tuple(Origin(t, None) for t in sorted({names.get(n, n) for n in _tables(select)}))
    return Provenance(column, kind, shown, origins, filters, grouped_by, traced)


def _projection(select: exp.Select, column: str) -> exp.Expression | None:
    for projection in select.expressions:
        if projection.alias_or_name == column:
            return projection
    return None


def _kind(projection: exp.Expression | None) -> str:
    if projection is None:
        return "column"  # from SELECT *
    inner = projection.unalias()
    if inner.find(exp.AggFunc):
        return "aggregate"
    return "column" if isinstance(inner, exp.Column) else "calculated"


def _origins(node: Any) -> tuple[Origin, ...]:
    found: list[Origin] = []
    for step in node.walk():
        if not isinstance(step.source, exp.Table) or step.downstream:
            continue
        name = step.name.split(".")[-1]
        origin = Origin(step.source.name, None if name == "*" else name)
        if origin not in found:
            found.append(origin)
    return tuple(found)


def _tables(select: exp.Select) -> list[str]:
    return [t.name for t in select.find_all(exp.Table)]


def _real_names(tree: exp.Expression) -> dict[str, str]:
    """Alias -> table name for every table the query reads (a CTE's name stays as it is)."""
    return {t.alias_or_name: t.name for t in tree.find_all(exp.Table) if t.alias}


def _with_real_names(node: exp.Expression, names: Mapping[str, str], dialect: str) -> str:
    def rename(part: exp.Expression) -> exp.Expression:
        if isinstance(part, exp.Column) and part.table in names:
            return exp.column(part.name, table=names[part.table])
        return part
    return node.copy().transform(rename).sql(dialect=dialect)


def _filters(tree: exp.Expression, names: Mapping[str, str], dialect: str) -> list[Filter]:
    """Every join condition, WHERE and HAVING condition (split on AND) and LIMIT, from each SELECT in the query."""
    found: list[Filter] = []
    for select in tree.find_all(exp.Select):
        tables = set(_tables(select))
        single = next(iter(tables)) if len(tables) == 1 else None
        for join in select.args.get("joins") or []:
            if join.args.get("on") is not None:
                found.append(Filter("join", _condition(join.args["on"], single, names, dialect)))
        for clause in ("where", "having"):
            part = select.args.get(clause)
            if part is not None:
                parts = part.this.flatten() if isinstance(part.this, exp.And) else [part.this]
                found.extend(Filter(clause, _condition(c, single, names, dialect)) for c in parts)
        limit = select.args.get("limit")
        if limit is not None:
            found.append(Filter("limit", limit.expression.sql(dialect=dialect)))
    return found


def _condition(node: exp.Expression, single: str | None, names: Mapping[str, str], dialect: str) -> str:
    """A condition as SQL with real table names; with one table, bare columns are named too ("orders.total > 10")."""
    if single is not None:
        node = node.copy().transform(
            lambda p: exp.column(p.name, table=single) if isinstance(p, exp.Column) and not p.table else p)
    return _with_real_names(node, names, dialect)


def _grouped_by(select: exp.Select, kind: str) -> tuple[str, ...]:
    group = select.args.get("group")
    if kind != "aggregate" or group is None:
        return ()
    keys = {g.sql() for g in group.expressions}
    out = []
    for projection in select.expressions:
        if projection.unalias().sql() in keys or projection.alias_or_name in keys:
            out.append(projection.alias_or_name)
    return tuple(out)
