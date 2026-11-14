"""The swapped-in table for a table with row rules (#132): guard.confined swaps every reference to a source table for
`(SELECT <allowed columns> FROM <table>) AS <name>`; on a table with row rules (grants.Cells) that inner SELECT also
keeps only the rows some applied profile reaches and gives each cell its own level:

    SELECT CASE WHEN <full rows> AND pw_r.c NOT IN (<values anonymised anywhere>) THEN pw_r.c
                WHEN <rows at least anonymised> THEN <stand-in of pw_r.c> END AS c,     -- anonymised somewhere
           CASE WHEN <full rows> THEN pw_r.d END AS d,                                  -- full or empty
           CASE WHEN <rows at least totals> THEN pw_r.e END AS e,                       -- totals somewhere
           CASE WHEN <full rows> THEN 0 WHEN <rows at least totals> THEN 1 ELSE 0 END AS pw_cell_e  -- totals.py
    FROM t AS pw_r WHERE <rows any profile reaches>

Because every query shape (`*`, joins, subqueries, WITH, UNION, window functions, aggregates) reads the table only
through this SELECT, a row no profile reaches doesn't exist for the AI, and a cell is never shown above its level.
"Values anonymised anywhere" is read from the same table under the same rules (`pw_a`), whatever the AI's own WHERE
says, so a value anonymised on one row is a stand-in on every row of the table. Conditions are decided the way CASE
decides them: a rule that can't be decided on a row (a NULL) doesn't match it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp

from portward_free.profiles import ANONYMISED, TOTALS
from portward_free.row_rules import NEVER, render

if TYPE_CHECKING:  # grants imports totals, which imports the guard, which imports this
    from portward_free.grants import Cells, ColumnCells, Rows

ROW, ANYWHERE = "pw_r", "pw_a"  # the table's own alias inside the swapped-in SELECT, and in its look-up of values
FLAG_PREFIX = "pw_cell_"  # + column: 1 on rows where that totals column's cell is totals only (totals.py counts them)
StandInSql = Callable[[str, str], str]


def flag_name(column: str) -> str:
    return FLAG_PREFIX + column.lower()


def _matches(rows: Rows, real: Mapping[str, str], alias: str) -> exp.Expression | None:
    """SQL true on the rows `rows` covers; None when it covers every row."""
    if rows is None:
        return None
    if not rows:
        return NEVER.copy()
    return exp.or_(*[render(r, real, alias) for r in rows])


def _when(condition: exp.Expression | None) -> exp.Expression:
    return condition if condition is not None else exp.EQ(this=exp.Literal.number(1), expression=exp.Literal.number(1))


def _case(*branches: tuple[exp.Expression | None, exp.Expression], default: exp.Expression | None = None
          ) -> exp.Expression:
    return exp.Case(ifs=[exp.If(this=_when(c), true=v) for c, v in branches], default=default)


def _col(column: str, alias: str) -> exp.Column:
    return exp.column(exp.to_identifier(column, quoted=True), table=exp.to_identifier(alias, quoted=True))


def _anonymised_somewhere(table: exp.Identifier, column: str, cells: ColumnCells,
                          real: Mapping[str, str]) -> exp.Expression:
    """The values of `column` on rows where its cell is anonymised (not full, at least anonymised)."""
    is_anonymised = _case((_matches(cells.full, real, ANYWHERE), exp.Literal.number(0)),
                          (_matches(cells.anonymised, real, ANYWHERE), exp.Literal.number(1)),
                          default=exp.Literal.number(0))
    where = exp.and_(exp.EQ(this=is_anonymised, expression=exp.Literal.number(1)),
                     exp.Not(this=exp.Is(this=_col(column, ANYWHERE), expression=exp.Null())))
    return exp.select(_col(column, ANYWHERE)).from_(
        exp.Table(this=table.copy(), alias=exp.TableAlias(this=exp.to_identifier(ANYWHERE, quoted=True)))
    ).where(where)


def _stand_in(column: str, group: str | None, dialect: str, stand_in_sql: StandInSql | None) -> exp.Expression:
    if group is None or stand_in_sql is None:
        return exp.Null()  # no stand-ins here: the cell is empty, never full
    return sqlglot.parse_one(stand_in_sql(_col(column, ROW).sql(dialect=dialect), group), read=dialect)


def _projection(table: exp.Identifier, column: str, cells: ColumnCells, real: Mapping[str, str], group: str | None,
                dialect: str, stand_in_sql: StandInSql | None) -> exp.Expression:
    value = _col(column, ROW)
    full = _matches(cells.full, real, ROW)
    if cells.shape == TOTALS:
        shown: exp.Expression = value if cells.any is None else _case((_matches(cells.any, real, ROW), value))
    elif cells.shape == ANONYMISED:
        stand_in = _stand_in(column, group, dialect, stand_in_sql)
        anonymised = _matches(cells.anonymised, real, ROW)
        if cells.full == ():
            shown = stand_in if cells.anonymised is None else _case((anonymised, stand_in))
        else:
            unique = exp.Not(this=exp.In(this=value.copy(), query=exp.Subquery(
                this=_anonymised_somewhere(table, column, cells, real))))
            shown = _case((exp.and_(_when(full), unique), value), (anonymised, stand_in))
    else:
        shown = value if full is None else _case((full, value))
    return exp.alias_(shown, exp.to_identifier(column, quoted=True))


def _flag(column: str, cells: ColumnCells, real: Mapping[str, str]) -> exp.Expression:
    flag = _case((_matches(cells.full, real, ROW), exp.Literal.number(0)),
                 (_matches(cells.any, real, ROW), exp.Literal.number(1)), default=exp.Literal.number(0))
    return exp.alias_(flag, exp.to_identifier(flag_name(column), quoted=True))


def inner(table: exp.Identifier, listed: Sequence[str], cells: Cells, dialect: str,
          stand_ins: Mapping[str, str] | None = None, stand_in_sql: StandInSql | None = None,
          extra: Sequence[exp.Expression] = (), flags: bool = False) -> exp.Select:
    """The swapped-in SELECT for `table` (as the query names it) with its `listed` (allowed) columns under `cells`.
    `stand_ins` maps its anonymised columns (lower case) to their link group; `extra` projections (a row number)
    are added as they are; `flags` adds a FLAG_PREFIX column per totals column."""
    real = {c.lower(): c for c in cells.real}
    by_name = cells.columns
    projections: list[exp.Expression] = []
    for column in listed:
        found = by_name.get(column.lower())
        if found is None:
            projections.append(exp.alias_(exp.Null(), exp.to_identifier(column, quoted=True)))
            continue
        projections.append(_projection(table, column, found, real, (stand_ins or {}).get(column.lower()), dialect,
                                       stand_in_sql))
    projections += list(extra)
    if flags:
        projections += [_flag(c, found, real) for c in listed
                        if (found := by_name.get(c.lower())) is not None and found.shape == TOTALS]
    select = exp.select(*projections).from_(
        exp.Table(this=table.copy(), alias=exp.TableAlias(this=exp.to_identifier(ROW, quoted=True))))
    rows = _matches(cells.rows, real, ROW)
    return select.where(rows) if rows is not None else select


def flagged(cells: Cells, listed: Sequence[str]) -> frozenset[str]:
    """The listed columns (lower case) that get a FLAG_PREFIX column."""
    by_name = cells.columns
    return frozenset(c.lower() for c in listed if (f := by_name.get(c.lower())) is not None and f.shape == TOTALS)
