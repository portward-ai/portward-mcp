"""Metrics at work (#129): writing a metric's SQL, deciding where a metric may be used, and checking SQL the AI
wrote against a metric's table.

- `compiled` writes the SQL for one metric question (grain, group-bys, dates) in the source's dialect. The same
  question gives the same SQL text, so the same number. The SQL then goes through the normal pull path (pull.run_pull),
  so the guard, levels, stand-ins, totals and the AI log apply to it as to any query.
- `hidden_names` says what keeps a metric from the AI on one connection: a column its own profile doesn't grant, one
  the AI can't see here, or a filter or time column it can't see in full (stand-ins or totals would make the filter
  wrong). No sealed metrics: a metric that needs a hidden column is held back, never run on the AI's behalf.
- `checked` compares the AI's SQL with a metric whose table it reads, when it computes the metric's measure or the
  question names the metric: are the metric's filters applied, and is it grouped only by columns the metric allows?
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from portward_free.context import mentions, ungranted
from portward_free.context import hides_something as _hides
from portward_free.metric_def import Metric, MetricError
from portward_free.profiles import FULL, Grant
from portward_free.rules import Policy
from portward_free.sources import Table

DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# SQLite has no DATE_TRUNC: each grain as a date string of the period's first day ({c} is the quoted column)
SQLITE_GRAINS = {
    "day": "STRFTIME('%Y-%m-%d', {c})",
    "week": "STRFTIME('%Y-%m-%d', {c}, '-6 days', 'weekday 1')",  # the Monday on or before
    "month": "STRFTIME('%Y-%m-01', {c})",
    "quarter": ("STRFTIME('%Y', {c}) || CASE WHEN STRFTIME('%m', {c}) <= '03' THEN '-01-01' WHEN STRFTIME('%m', {c}) "
                "<= '06' THEN '-04-01' WHEN STRFTIME('%m', {c}) <= '09' THEN '-07-01' ELSE '-10-01' END"),
    "year": "STRFTIME('%Y-01-01', {c})",
}


# --- where a metric may be used -------------------------------------------------------------------------------------

def _table(visible: Sequence[Table], name: str) -> Table | None:
    return next((t for t in visible if t.name.lower() == name.lower()), None)


def _granted(grants: Sequence[Grant], table: str, column: str) -> bool:
    return any(g.table.lower() == table.lower() and g.column.lower() == column.lower() for g in grants)


def hidden_names(metric: Metric, grants: Sequence[Grant], rules: Policy, visible: Sequence[Table],
                 catalog: Sequence[Table]) -> tuple[str, ...]:
    """The names that keep `metric` (of a profile granting `grants`) from the AI on a connection: empty when it may
    be used there."""
    table = _table(visible, metric.table)
    if table is None:
        return (metric.table,)
    real = {c.lower() for c in table.columns}
    exact = {c.lower() for c in metric.exact_columns}
    found = []
    for column in metric.columns:
        name = f"{metric.table}.{column}"
        if not _granted(grants, metric.table, column) or column.lower() not in real:
            found.append(name)
        elif column.lower() in exact and rules.shape(table, _real(table, column)) != FULL:
            found.append(name)
    found += [n for n in mentions(metric.text) if _hides(n, visible, catalog)]
    found += ungranted(metric.text, grants, [catalog])
    return tuple(dict.fromkeys(found))


def _real(table: Table, column: str) -> str:
    return next(c for c in table.columns if c.lower() == column.lower())


# --- writing the SQL ------------------------------------------------------------------------------------------------

def _quoted(table: Table, node: exp.Expression) -> exp.Expression:
    """`node` with each column renamed to its real name in `table`, quoted."""
    def real(n: exp.Expression) -> exp.Expression:
        if isinstance(n, exp.Column):
            return exp.column(exp.to_identifier(_real(table, n.name), quoted=True))
        return n
    return node.copy().transform(real)


def _truncated(column: exp.Expression, grain: str, dialect: str) -> exp.Expression:
    quoted = column.sql(dialect=dialect)
    if dialect == "sqlite":
        return sqlglot.parse_one(SQLITE_GRAINS[grain].format(c=quoted), read="sqlite")
    return exp.DateTrunc(this=column.copy(), unit=exp.Literal.string(grain))


def compiled(metric: Metric, table: Table, dialect: str, grain: str | None = None,
             group_by: Sequence[str] = (), start: str | None = None, end: str | None = None) -> str:
    """The SQL for one metric question on `table` (the visible table it reads), in `dialect`. MetricError, safe to
    show the AI, when the question asks for something the metric doesn't allow."""
    grain = grain.strip().lower() if grain and grain.strip() else None
    if (grain or start or end) and not metric.time:
        raise MetricError(f"metric {metric.name!r} has no time column, so it can't be cut by time")
    if grain and grain not in metric.grains:
        raise MetricError(f"metric {metric.name!r} may be cut by {', '.join(metric.grains)}, not {grain!r}")
    allowed = {g.lower(): g for g in metric.group_by}
    wanted = list(dict.fromkeys(g.strip().lower() for g in group_by if g.strip()))
    if refused := [g for g in wanted if g not in allowed]:
        may = ", ".join(metric.group_by) or "nothing"
        raise MetricError(f"metric {metric.name!r} may be grouped by {may}, not {refused[0]!r}")
    for name, value in (("start", start), ("end", end)):
        if value is not None and not DATE.fullmatch(value.strip()):
            raise MetricError(f"{name} must be a date like 2026-11-01")
    keys: list[exp.Expression] = []
    if grain:
        keys.append(exp.alias_(_truncated(_quoted(table, exp.column(metric.time)), grain, dialect), grain,
                               quoted=True))
    keys += [_quoted(table, exp.column(allowed[g])) for g in wanted]
    measure = exp.alias_(_quoted(table, sqlglot.parse_one(metric.measure)), metric.name, quoted=True)
    conditions = [exp.paren(_quoted(table, sqlglot.parse_one(f)), copy=False) for f in metric.filters]
    if metric.time:
        column = _quoted(table, exp.column(metric.time))
        if start:
            conditions.append(exp.GTE(this=column.copy(), expression=exp.Literal.string(start.strip())))
        if end:
            conditions.append(exp.LT(this=column.copy(), expression=exp.Literal.string(end.strip())))
    query = exp.select(*keys, measure).from_(exp.Table(this=exp.to_identifier(table.name, quoted=True)))
    if conditions:
        query = query.where(exp.and_(*conditions))
    if keys:
        positions = [exp.Literal.number(i + 1) for i in range(len(keys))]
        query = query.group_by(*positions).order_by(*[p.copy() for p in positions])
    return query.sql(dialect=dialect)


# --- checking the AI's SQL ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class MetricCheck:
    metric: str
    profile: str
    version: int
    missing_filters: tuple[str, ...] = ()
    not_allowed: tuple[str, ...] = ()  # columns it was grouped by that the metric doesn't allow
    measure_missing: bool = False

    @property
    def followed(self) -> bool:
        return not (self.missing_filters or self.not_allowed or self.measure_missing)

    def notice(self, measure: str) -> str:
        named = f"metric {self.metric} (v{self.version}, profile {self.profile!r})"
        if self.followed:
            return f"This SQL follows {named}: its filters are applied."
        said = []
        if self.missing_filters:
            said.append(f"it leaves out the filter{'s' * (len(self.missing_filters) > 1)} "
                        + " and ".join(self.missing_filters))
        if self.not_allowed:
            said.append(f"it groups by {', '.join(self.not_allowed)}, which the metric doesn't allow")
        if self.measure_missing:
            said.append(f"it doesn't compute {measure}")
        return (f"This SQL doesn't follow {named}: {'; '.join(said)}. Tell the user the answer doesn't follow "
                f"{self.metric}, or use the metric tool with name {self.metric!r} for the number as defined.")

    def payload(self) -> dict[str, object]:
        return {"metric": self.metric, "profile": self.profile, "version": self.version, "followed": self.followed,
                "missing_filters": list(self.missing_filters), "not_allowed_group_by": list(self.not_allowed),
                "measure_missing": self.measure_missing}


def _canonical(node: exp.Expression) -> str:
    """One condition or aggregate as plain SQL, whatever the table qualifiers, quotes, case of names or brackets."""
    while isinstance(node, exp.Paren):
        node = node.this

    def plain(n: exp.Expression) -> exp.Expression:
        if isinstance(n, exp.Column):
            return exp.column(n.name.lower())
        if isinstance(n, exp.Paren):
            return n.this
        if isinstance(n, (exp.EQ, exp.NEQ)) and isinstance(n.this, exp.Literal) \
                and not isinstance(n.expression, exp.Literal):
            return n.__class__(this=n.expression, expression=n.this)
        return n
    return node.copy().transform(plain).sql()


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        return [*_conjuncts(node.this), *_conjuncts(node.expression)]
    return [node]


def _conditions(tree: exp.Expression) -> set[str]:
    found: list[exp.Expression] = []
    for clause in (*tree.find_all(exp.Where), *tree.find_all(exp.Having)):
        found += _conjuncts(clause.this)
    for join in tree.find_all(exp.Join):
        found += _conjuncts(join.args.get("on"))
    return {_canonical(c) for c in found}


def _grouped_columns(tree: exp.Expression) -> set[str]:
    """The source columns the query groups by (an ordinal or an alias is followed to its select item)."""
    names: set[str] = set()
    for group in tree.find_all(exp.Group):
        select = group.parent
        items = select.expressions if isinstance(select, exp.Select) else []
        aliases = {i.alias.lower(): i.unalias() for i in items if i.alias}
        for key in group.expressions:
            if isinstance(key, exp.Literal) and key.is_int and 0 < int(key.this) <= len(items):
                key = items[int(key.this) - 1].unalias()
            elif isinstance(key, exp.Column) and not key.table and key.name.lower() in aliases:
                key = aliases[key.name.lower()]
            names.update(c.name.lower() for c in key.find_all(exp.Column))
            if isinstance(key, exp.Column):
                names.add(key.name.lower())
    return names


def _names_metric(question: str, metric: Metric) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(metric.name.lower())}(?![A-Za-z0-9_])",
                     question.lower()) is not None


def checked(sql: str, dialect: str, metric: Metric, profile: str, question: str) -> MetricCheck | None:
    """How the AI's `sql` measures up to `metric`, or None when it doesn't read the metric's table, or reads it
    without computing the metric's measure in answer to a question that doesn't name it."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except (SqlglotError, ValueError):
        return None
    if not any(t.name.lower() == metric.table.lower() for t in tree.find_all(exp.Table)):
        return None
    measure = _canonical(sqlglot.parse_one(metric.measure))
    computes = any(_canonical(a) == measure for a in tree.find_all(exp.AggFunc))
    if not computes and not _names_metric(question, metric):
        return None
    have = _conditions(tree)
    missing = tuple(f for f in metric.filters
                    if not all(_canonical(c) in have for c in _conjuncts(sqlglot.parse_one(f))))
    allowed = {g.lower() for g in metric.group_by} | ({metric.time.lower()} if metric.time else set())
    not_allowed = tuple(sorted(_grouped_columns(tree) - allowed))
    return MetricCheck(metric.name, profile, metric.version, missing, not_allowed, not computes)
