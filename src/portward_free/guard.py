"""Checks AI-written SQL before it runs: one plain SELECT over the source's own tables, nothing else.

Lifted from the gateway's guard and trimmed: the parsing, the blocked node types, the
function allow-list and the table/CTE rules are the same; tokenization is left out, totals-only columns (the
gateway's aggregates-only) are checked in totals.py, and anonymised columns (stand-ins) are swapped in by `confined`,
the gateway's tokenized rewrite in this guard's shape. Sources also open read-only connections, so the guard is one
of two locks, not the only one.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from portward_free import row_cells

if TYPE_CHECKING:
    from portward_free.grants import Cells

# Functions the AI may call in addition to the ones sqlglot models natively.
# Anything sqlglot cannot classify (exp.Anonymous) must be on this list.
SAFE_ANONYMOUS_FUNCTIONS = frozenset(
    {"date_trunc", "date_part", "to_char", "initcap", "split_part", "strftime", "make_date", "age"}
)
BLOCKED_NODE_TYPES = (
    exp.Command, exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Merge, exp.Into,
    exp.Pragma, exp.Attach, exp.Detach,
    # Table-producing constructs that are not plain table names.
    exp.Lateral, exp.Unnest, exp.ReadCSV, exp.ReadParquet, exp.GenerateSeries, exp.ExplodingGenerateSeries,
    exp.TableFromRows, exp.Values,
    exp.Lock,  # SELECT ... FOR UPDATE / FOR SHARE takes row locks
)
VARIABLE_NODE_TYPES = (exp.Parameter, exp.SessionParameter, exp.Placeholder)
ROW_ID = "pw_rowid"  # a row number `confined` adds to tables with totals-only columns, so groups count real rows


class QueryRejected(Exception):
    """The SQL is not allowed. The message is safe to show to the AI."""


@dataclass(frozen=True)
class CheckedQuery:
    tree: exp.Expression
    tables: tuple[str, ...]  # the source tables the query reads, lower case
    columns: tuple[str, ...]  # every column name referenced, lower case
    real_refs: tuple[exp.Table, ...] = ()  # the nodes of `tree` that name a source table (not a WITH name)


def _parse_single(sql: str, dialect: str) -> exp.Expression:
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except ParseError as err:
        raise QueryRejected(f"could not parse SQL: {err.errors[0]['description'] if err.errors else err}") from None
    if len(statements) != 1:
        raise QueryRejected("send exactly one SELECT statement")
    if not isinstance(statements[0], exp.Query):
        raise QueryRejected("only SELECT queries are allowed")
    return statements[0]


def _check_nodes(tree: exp.Expression) -> None:
    for node in tree.walk():
        if isinstance(node, BLOCKED_NODE_TYPES):
            raise QueryRejected(f"{node.key.upper()} is not allowed; use plain SELECT over tables from list_sources")
        if isinstance(node, VARIABLE_NODE_TYPES):
            raise QueryRejected("variables and parameters (@name, @@name, $name, ?) are not allowed; write values inline")
        if isinstance(node, exp.Anonymous) and node.name.lower() not in SAFE_ANONYMOUS_FUNCTIONS:
            raise QueryRejected(f"function {node.name!r} is not allowed")
        if isinstance(node, exp.Table) and not isinstance(node.this, exp.Identifier):
            raise QueryRejected("only plain table names are allowed in FROM and JOIN")
        if isinstance(node, exp.Table) and (node.args.get("db") or node.args.get("catalog")):
            raise QueryRejected("use bare table names from list_sources, without a schema")


def _top_level_ctes(tree: exp.Expression, reserved: set[str]) -> list[exp.CTE]:
    withs = list(tree.find_all(exp.With))
    if not withs:
        return []
    top = tree.args.get("with_") or tree.args.get("with")  # key name differs across sqlglot versions
    if len(withs) > 1 or top is None or withs[0] is not top:
        raise QueryRejected("only one WITH clause, at the start of the query, is allowed")
    if top.args.get("recursive"):
        raise QueryRejected("WITH RECURSIVE is not allowed")
    ctes = list(top.expressions)
    seen: set[str] = set()
    for cte in ctes:
        name = cte.alias_or_name.lower()
        if name in reserved:
            raise QueryRejected(f"a WITH name may not reuse the table name {cte.alias_or_name!r}")
        if name in seen:
            raise QueryRejected(f"two WITH names differ only by case or quotes: {cte.alias_or_name!r}")
        seen.add(name)
    return ctes


def _classify(tables: list[exp.Table], visible_ctes: set[str], allowed: set[str], real: list[exp.Table],
              withheld: Collection[str] = ()) -> None:
    for table in tables:
        name = table.name.lower()
        if name in visible_ctes:
            continue
        if name in withheld:
            raise QueryRejected(f"table {table.name} is blocked by the user's rules; don't query it")
        if name not in allowed:
            raise QueryRejected(f"table {table.name!r} is not available; call list_sources")
        real.append(table)


def check(sql: str, dialect: str, allowed_tables: Sequence[str], known_tables: Sequence[str] = ()) -> CheckedQuery:
    """Parse and validate; refuse anything that is not one SELECT over `allowed_tables`.

    `known_tables` are all the source's table names; the ones not in `allowed_tables` are blocked: a query naming one
    is refused as blocked, and a WITH name may not reuse one.
    """
    tree = _parse_single(sql, dialect)
    _check_nodes(tree)
    allowed = {t.lower() for t in allowed_tables}
    withheld = {t.lower() for t in known_tables} - allowed
    ctes = _top_level_ctes(tree, allowed | withheld)
    real: list[exp.Table] = []
    in_cte: set[int] = set()
    defined: set[str] = set()
    for cte in ctes:  # a CTE body sees only the CTEs defined before it
        body_tables = list(cte.this.find_all(exp.Table))
        in_cte.update(id(t) for t in body_tables)
        _classify(body_tables, set(defined), allowed, real, withheld)
        defined.add(cte.alias_or_name.lower())
    _classify([t for t in tree.find_all(exp.Table) if id(t) not in in_cte], defined, allowed, real, withheld)
    if not real:
        raise QueryRejected("the query must read at least one table from list_sources")
    tables = tuple(dict.fromkeys(t.name.lower() for t in real))
    columns = tuple(sorted({c.name.lower() for c in tree.find_all(exp.Column) if c.name}))
    return CheckedQuery(tree=tree, tables=tables, columns=columns, real_refs=tuple(real))


StandInSql = Callable[[str, str], str]  # the quoted column's SQL, its link group -> its stand-in's SQL (sources)


def confined(checked: CheckedQuery, dialect: str, columns: Mapping[str, Sequence[str]], max_rows: int,
             row_id_tables: Collection[str] = (), stand_ins: Mapping[str, Mapping[str, str]] | None = None,
             stand_in_sql: StandInSql | None = None, cells: Mapping[str, Cells] | None = None) -> str:
    """The SQL to run: each source table swapped for `SELECT <its allowed columns> FROM <table>` under the same
    name, wrapped so at most `max_rows` rows come back.

    `stand_ins` maps a table (lower case) to its columns (lower case) the AI sees only as stand-ins, each to its link
    group (stand_ins.Scopes): in the swapped-in table such a column holds `stand_in_sql` of the real one in that group
    under the same name, so every query shape reads the stand-in and never the value.

    `cells` maps each table with row rules (lower case) to its grants.Cells: its swapped-in table keeps only the rows
    some applied profile reaches, each cell at its own level (row_cells.py), with a totals flag per totals column
    when it is in `row_id_tables`.

    `columns` maps each allowed table (lower case) to the columns the AI may read. The database itself then refuses
    a blocked column however the query reaches it (`*`, `t.*`, joins, aliases, subqueries, WITH, expressions,
    WHERE, ORDER BY, whole-row references), since the swapped-in table has no such column. A table missing from
    `columns` is refused. Tables in `row_id_tables` (lower case) also get a ROW_ID row number (totals.py).
    """
    tree = checked.tree.copy()
    copies = {id(old): new for old, new in zip(checked.tree.walk(), tree.walk(), strict=True)}
    for ref in checked.real_refs:
        name = ref.name.lower()
        listed = columns.get(name)
        if not listed:
            raise QueryRejected(f"table {ref.name!r} is not available; call list_sources")
        # the query's own identifiers, quoting kept, so the names resolve exactly as the AI wrote them
        named = ref.args["alias"].this if ref.args.get("alias") else ref.this
        shown = (stand_ins or {}).get(name, {})
        extra = [_row_number(dialect)] if name in row_id_tables else []
        if cells and name in cells:  # row rules: the rows any profile reaches, each cell at its own level
            if any(shown.get(c.lower()) for c in listed) and stand_in_sql is None:
                raise QueryRejected(f"table {ref.name!r} is not available; call list_sources")
            inner = row_cells.inner(ref.this, listed, cells[name], dialect, shown, stand_in_sql, extra,
                                    flags=name in row_id_tables)
        else:
            projections: list[exp.Expression] = [_inner_column(c, shown.get(c.lower()), dialect, stand_in_sql)
                                                 for c in listed]
            inner = exp.select(*projections, *extra).from_(exp.Table(this=ref.this.copy()))
        copies[id(ref)].replace(exp.Subquery(this=inner, alias=exp.TableAlias(this=named.copy())))
    wrapper = exp.Subquery(this=tree, alias=exp.TableAlias(this=exp.to_identifier("pw_q")))
    return exp.select("*").from_(wrapper).limit(max_rows).sql(dialect=dialect)


def _row_number(dialect: str) -> exp.Expression:
    """ROW_NUMBER() OVER () AS ROW_ID: any order gives every row its own id."""
    return exp.alias_(exp.Window(this=exp.RowNumber()), ROW_ID, quoted=True)


def _inner_column(column: str, group: str | None, dialect: str, stand_in_sql: StandInSql | None) -> exp.Expression:
    """One column of a swapped-in table: the column itself, or (with a link group) its stand-in under the column's
    own name."""
    named = exp.to_identifier(column, quoted=True)
    if group is None:
        return exp.column(named)
    if stand_in_sql is None:
        raise QueryRejected(f"column {column!r} is not available; call list_sources")
    real = exp.column(named.copy()).sql(dialect=dialect)
    return exp.alias_(sqlglot.parse_one(stand_in_sql(real, group), read=dialect), named)
