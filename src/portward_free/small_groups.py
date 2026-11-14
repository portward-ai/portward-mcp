"""Small groups merge into one row (#122): a grouped totals query keeps every group that reaches the minimum and
merges the rest into one 'other (small groups)' row, as Snowflake's aggregation policies do, so the totals still add
up and the AI can't tell which groups were merged. The merged row is itself held to the minimum (counted over its
own rows and people, so one person spread over many small groups is still one person) and left out when under it.

totals.py calls `merged` with the copied query and the threshold conditions it would otherwise put in HAVING. The
query becomes, in the source's own SQL:

    WITH pw_rows AS (SELECT <group keys>, <columns the totals and conditions read> FROM <as written> WHERE <as written>),
         pw_groups AS (SELECT <group keys>, CASE WHEN <conditions> THEN 0 ELSE 1 END AS pw_small FROM pw_rows GROUP BY ..)
    SELECT <as written, keys NULL on the merged row>, pw_small AS pw_other FROM pw_rows JOIN pw_groups ON <same keys>
    GROUP BY pw_small, <keys> HAVING <conditions> ORDER BY <as written> LIMIT <as written>

`labelled` then writes the label into the merged row's group columns and drops OTHER_FLAG.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlglot import exp

from portward_free.guard import QueryRejected

OTHER = "other (small groups)"
OTHER_FLAG = "pw_other"  # last result column: 1 on the merged row
ROWS, GROUPS, SMALL = "pw_rows", "pw_groups", "pw_small"
RESERVED = frozenset({ROWS, GROUPS, SMALL, OTHER_FLAG})


@dataclass(frozen=True)
class Merged:
    tree: exp.Select
    labels: tuple[int, ...]  # result columns that show a group key, labelled OTHER on the merged row


class _Rows:
    """The columns pw_rows projects: each group key and each column an aggregate or condition reads, by its SQL."""

    def __init__(self) -> None:
        self.projections: list[exp.Expression] = []
        self.names: dict[str, str] = {}

    def name(self, expression: exp.Expression, prefix: str) -> str:
        key = expression.sql()
        if key not in self.names:
            self.names[key] = f"pw_{prefix}{len(self.names)}"
            self.projections.append(exp.alias_(expression.copy(), self.names[key], quoted=True))
        return self.names[key]


class _KeyMatch:
    """Finds which group key an expression is: written the same way, or the same once table names are left off (when
    that matches one key only), so `region` matches GROUP BY o.region."""

    def __init__(self, keys: Sequence[exp.Expression]) -> None:
        self.exact = {k.sql(): i for i, k in enumerate(keys)}
        loose: dict[str, list[int]] = {}
        for i, k in enumerate(keys):
            loose.setdefault(_unqualified(k), []).append(i)
        self.loose = {sql: found[0] for sql, found in loose.items() if len(found) == 1}

    def __contains__(self, node: object) -> bool:
        return self.get(node) is not None  # type: ignore[arg-type]

    def get(self, node: exp.Expression) -> int | None:
        found = self.exact.get(node.sql())
        return found if found is not None else self.loose.get(_unqualified(node))


def _unqualified(expression: exp.Expression) -> str:
    copied = expression.copy()
    for column in copied.find_all(exp.Column):
        column.set("table", None)
    return copied.sql()


def _col(name: str, table: str = ROWS) -> exp.Column:
    return exp.column(exp.to_identifier(name, quoted=True), table=exp.to_identifier(table))


def merged(tree: exp.Select, conditions: Sequence[exp.Expression]) -> Merged:
    """`tree` (a copy, taken apart) rewritten so groups failing `conditions` merge into one row."""
    keys = _keys(tree)
    rows = _Rows()
    key_names = [rows.name(k, "k") for k in keys]
    key_sql = _KeyMatch(keys)
    small = _col(SMALL, GROUPS)

    def merged_key(i: int) -> exp.Expression:
        return exp.Case(ifs=[exp.If(this=exp.EQ(this=small.copy(), expression=exp.Literal.number(1)),
                                    true=exp.Null())], default=_col(key_names[i]))

    aliases = {s.alias.lower() for s in tree.expressions if isinstance(s, exp.Alias)}
    outer_items, labels = [], []
    for position, item in enumerate(tree.expressions):
        inner = item.this if isinstance(item, exp.Alias) else item
        name = item.alias if isinstance(item, exp.Alias) else (inner.name if isinstance(inner, exp.Column)
                                                               else inner.sql())
        rewritten, keyed = _rewrite(inner, rows, key_sql, merged_key, frozenset())
        if keyed and not any(isinstance(n, (exp.AggFunc, exp.Filter)) for n in inner.walk()):
            labels.append(position)
        outer_items.append(exp.alias_(rewritten, name, quoted=True))
    outer_items.append(exp.alias_(small.copy(), OTHER_FLAG, quoted=True))
    order = tree.args.get("order")
    ordered = [exp.Ordered(this=_rewrite(o.this, rows, key_sql, merged_key, aliases)[0], desc=o.args.get("desc"),
                           nulls_first=o.args.get("nulls_first")) for o in order.expressions] if order else []
    having = [_on_rows(c, rows) for c in conditions]

    pw_rows = exp.Select(expressions=rows.projections)
    for part in ("from_", "joins", "where"):
        if tree.args.get(part) is not None:
            pw_rows.set(part, tree.args[part])
    group_cols = [_col(n) for n in key_names]
    pw_groups = exp.select(*group_cols, exp.alias_(
        exp.Case(ifs=[exp.If(this=exp.and_(*[h.copy() for h in having]), true=exp.Literal.number(0))],
                 default=exp.Literal.number(1)), SMALL, quoted=True)).from_(ROWS).group_by(*[c.copy() for c in group_cols])
    joined = exp.and_(*[exp.NullSafeEQ(this=_col(n), expression=_col(n, GROUPS)) for n in key_names])
    outer = exp.select(*outer_items).from_(ROWS).join(GROUPS, on=joined) \
        .group_by(small.copy(), *[merged_key(i) for i in range(len(keys))]).having(exp.and_(*having))
    if ordered:
        outer.set("order", exp.Order(expressions=ordered))
    for part in ("limit", "offset", "distinct"):
        if tree.args.get(part) is not None:
            outer.set(part, tree.args[part])
    outer = outer.with_(ROWS, as_=pw_rows, copy=False).with_(GROUPS, as_=pw_groups, copy=False)
    return Merged(outer, tuple(labels))


def _keys(tree: exp.Select) -> list[exp.Expression]:
    """The GROUP BY expressions, with positions (GROUP BY 1) and select aliases (GROUP BY r) resolved."""
    items = tree.expressions
    by_alias = {s.alias.lower(): s.this for s in items if isinstance(s, exp.Alias)}
    found = []
    for key in tree.args["group"].expressions:
        if isinstance(key, exp.Literal) and key.is_int and 1 <= int(key.name) <= len(items):
            chosen = items[int(key.name) - 1]
            key = chosen.this if isinstance(chosen, exp.Alias) else chosen
        elif isinstance(key, exp.Column) and not key.table and key.name.lower() in by_alias:
            key = by_alias[key.name.lower()]
        if any(isinstance(n, (exp.AggFunc, exp.Filter)) for n in key.walk()):
            raise QueryRejected("GROUP BY a total isn't allowed with totals-only columns")
        found.append(key)
    return found


def _rewrite(expression: exp.Expression, rows: _Rows, key_sql: _KeyMatch, merged_key: Any,
             aliases: frozenset[str] | set[str]) -> tuple[exp.Expression, bool]:
    """`expression` for the outer query: group keys become the merged key, the columns inside each aggregate read
    pw_rows, and any other column is refused. Also whether it shows a group key."""
    holder = exp.Paren(this=expression.copy())
    keyed = False
    targets: list[exp.Expression] = []

    def collect(node: exp.Expression) -> None:
        if isinstance(node, (exp.AggFunc, exp.Filter)) or node in key_sql or isinstance(node, exp.Column):
            targets.append(node)
            return
        for child in node.iter_expressions():
            collect(child)

    collect(holder.this)
    for node in targets:
        if isinstance(node, (exp.AggFunc, exp.Filter)):
            for column in list(node.find_all(exp.Column)):
                column.replace(_col(rows.name(column, "v")))
        elif (index := key_sql.get(node)) is not None:
            keyed = True
            node.replace(merged_key(index))
        elif not node.table and node.name.lower() in aliases:
            continue  # ORDER BY a result column's name
        else:
            raise QueryRejected(f"{node.sql()} is neither in GROUP BY nor inside SUM, AVG or COUNT; with totals-only "
                                "columns every other column must be in GROUP BY")
    return holder.this, keyed


def _on_rows(condition: exp.Expression, rows: _Rows) -> exp.Expression:
    copied = exp.Paren(this=condition.copy())
    for column in list(copied.find_all(exp.Column)):
        column.replace(_col(rows.name(column, "v")))
    return copied.this


def labelled(columns: tuple[str, ...], rows: Sequence[tuple[Any, ...]], labels: Sequence[int]
             ) -> tuple[tuple[str, ...], tuple[tuple[Any, ...], ...]]:
    """The result without OTHER_FLAG, the merged row's group columns saying OTHER."""
    if not columns or columns[-1] != OTHER_FLAG:
        raise QueryRejected("the totals result came back in an unexpected shape")
    shown = []
    for row in rows:
        *values, flag = row
        if flag:
            values = [OTHER if i in labels else v for i, v in enumerate(values)]
        shown.append(tuple(values))
    return columns[:-1], tuple(shown)
