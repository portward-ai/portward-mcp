"""Totals-only columns (#94, Personal): a column a profile grants as totals is read only as the bare argument of
SUM, AVG or COUNT, in one plain SELECT, and every result row must cover at least MIN_GROUP_SIZE different rows and
values; smaller groups are dropped by a HAVING clause the query gets before it runs.

Ported from the gateway's guard (`check_aggregates`), which free must not import. The pull
(pull.py) calls `limit` after guard.check and before guard.confined; `confined(..., row_id_tables=...)` adds the
ROW_ID column the threshold counts. Known limit, as in the gateway: two large groups that differ by one row can still
be subtracted (differencing); the threshold stops direct look-ups, not every inference.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlglot import exp

from portward_free import row_cells, small_groups
from portward_free.guard import ROW_ID, CheckedQuery, QueryRejected
from portward_free.sources import SourceError

MIN_GROUP_SIZE = 5  # a total over fewer rows (or fewer different values) than this is left out
AGGREGATES = (exp.Sum, exp.Avg, exp.Count)
PLAIN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")  # folds the same way in Python and in every engine


def note(counted: str, min_group: int = MIN_GROUP_SIZE) -> str:
    return f"Groups covering fewer than {min_group} {counted} are left out (totals-only columns)."


NOTE = note("rows")


@dataclass(frozen=True)
class Totalled:
    """A query that reads a totals-only column, with the threshold added, and what the AI should be told."""

    query: CheckedQuery
    note: str
    labels: tuple[int, ...] | None = None  # grouped: small groups merged, these result columns show a group key

    def shown(self, columns: tuple[str, ...], rows: Sequence[tuple]) -> tuple[tuple[str, ...], tuple[tuple, ...]]:
        """The result as the AI gets it: on a grouped query, the merged row labelled and the marker column gone."""
        if self.labels is None:
            return columns, tuple(rows)
        return small_groups.labelled(columns, rows, self.labels)


def limit(checked: CheckedQuery, totals: Mapping[str, frozenset[str]], columns: Mapping[str, Sequence[str]],
          people: Mapping[str, frozenset[str]] | None = None, min_group: int = MIN_GROUP_SIZE,
          flagged: Mapping[str, frozenset[str]] | None = None) -> Totalled | None:
    """The query with the small-group threshold added, if it reads a totals-only column; None if it reads none.
    QueryRejected, with a plain reason, if it uses one any other way.

    `totals` maps tables (lower case) to their totals-only columns (lower case); `columns` maps the tables the AI may
    read to their visible columns; `people` maps tables to their person columns (from the tables' meanings), counted
    instead of rows. `checked` is left as it was.

    `flagged` maps tables with row rules (#132) to their totals columns whose cells are not all totals only (some
    full, some empty): the rewrite flags each row whose cell is totals only (row_cells.FLAG_PREFIX), and a group
    passes when it holds no such cell of a column it totals, or at least `min_group` rows (people) and values among
    those cells. Full cells never count towards the minimum, so adding one small set of totals cells to a group of
    full ones can't be subtracted back out.
    """
    people = people or {}
    flagged = flagged or {}
    protected = {t for t, cols in totals.items() if cols}
    if not protected.intersection(checked.tables):
        return None
    tree = checked.tree
    for star in tree.find_all(exp.Star):
        if not isinstance(star.parent, exp.Count):
            raise QueryRejected("SELECT * isn't allowed on a table with totals-only columns; list the columns you need")
    table_names = set(checked.tables) | {r.alias_or_name.lower() for r in checked.real_refs}
    real_columns = {c.lower() for t in checked.tables for c in columns.get(t, ())}
    _check_plain_columns(tree, real_columns, table_names)
    protected_columns = set().union(*(totals[t] for t in protected.intersection(checked.tables)))
    refs = [c for c in tree.find_all(exp.Column) if c.name.lower() in protected_columns]
    if not refs:
        return None
    names = ", ".join(sorted({c.name for c in refs}))
    _check_subtraction(tree, names)
    counted = {t: people.get(t, frozenset()) for t in protected.intersection(checked.tables)}
    _check_people(counted, columns)
    persons = set().union(*counted.values())
    flags = {row_cells.flag_name(c) for cols in flagged.values() for c in cols}
    _check_shape(tree, refs, names, table_names | protected_columns | persons | flags, checked)
    query, labels = _with_threshold(checked, refs, totals, counted, min_group, flagged)
    counting = "people" if persons else "rows"
    if labels is None:
        return Totalled(query, note(counting, min_group))
    return Totalled(query, f"Groups covering fewer than {min_group} {counting} are merged into one "
                           f"'{small_groups.OTHER}' row, itself left out if it covers fewer than {min_group} "
                           "(totals-only columns).", labels)


def _check_people(counted: Mapping[str, frozenset[str]], columns: Mapping[str, Sequence[str]]) -> None:
    for table, persons in counted.items():
        visible = {c.lower() for c in columns.get(table, ())}
        for person in sorted(persons - visible):
            raise QueryRejected(f"totals of {table} count people by {person}, which isn't available here; grant "
                                f"{table}.{person} or take it off as the table's person column")


def withheld(err: SourceError) -> SourceError:
    """A failed query that read a totals-only column: the threshold runs in the database after values are read, so
    the database's message could quote one. Only that it failed is kept."""
    return SourceError("the query failed. The database's message is withheld because this query reads a totals-only "
                       f"column and the message could quote its values ({type(err).__name__})")


def _check_plain_columns(tree: exp.Expression, real_columns: set[str], tables: set[str]) -> None:
    """Every column reference must be a visible column or a select alias: no whole-row references, field access,
    COLUMNS(...), JOIN ... USING or NATURAL JOIN (they name columns without a Column node)."""
    aliases = {a.alias.lower() for a in tree.find_all(exp.Alias) if a.alias}
    for join in tree.find_all(exp.Join):
        if join.args.get("using") or (join.args.get("method") or "").upper() == "NATURAL":
            raise QueryRejected("JOIN ... USING and NATURAL JOIN aren't allowed with totals-only columns; join ON "
                                "explicit columns")
    for node in tree.walk():
        if isinstance(node, (exp.Dot, exp.Bracket)) or type(node).__name__ == "Columns":
            raise QueryRejected("field access and COLUMNS(...) aren't allowed with totals-only columns; name columns "
                                "directly")
        if isinstance(node, exp.Column):
            name = node.name.lower()
            if not node.table and name in tables:
                raise QueryRejected(f"{node.name!r} is a table; select its columns by name")
            if name not in real_columns and name not in aliases:
                raise QueryRejected(f"unknown column {node.name!r}; call describe_source")


SET_OPERATIONS = ((exp.Union, "UNION"), (exp.Intersect, "INTERSECT"), (exp.Except, "EXCEPT"))
GROUPINGS = ((exp.Rollup, "ROLLUP"), (exp.Cube, "CUBE"), (exp.GroupingSets, "GROUPING SETS"))


def _check_subtraction(tree: exp.Expression, names: str) -> None:
    """Refuse the shapes that put two overlapping totals side by side, so one can be subtracted from the other to
    read a small group: set operations, ROLLUP / CUBE / GROUPING SETS (subtotals) and window functions."""
    for kind, word in SET_OPERATIONS:
        if any(isinstance(n, kind) for n in tree.walk()):
            raise QueryRejected(f"{names}: {word} isn't allowed with totals-only columns; send one query per total")
    group = tree.args.get("group")
    for kind, word in GROUPINGS:
        if group and any(isinstance(n, kind) for n in group.walk()):
            raise QueryRejected(f"{names}: {word} isn't allowed with totals-only columns (subtotals let one total be "
                                "subtracted from another); GROUP BY the columns themselves")
    if any(isinstance(n, exp.Window) for n in tree.walk()):
        raise QueryRejected(f"{names}: window functions (OVER ...) aren't allowed with totals-only columns; use "
                            "GROUP BY")


def _check_shape(tree: exp.Expression, refs: list[exp.Column], names: str, reserved: set[str],
                 checked: CheckedQuery) -> None:
    if not isinstance(tree, exp.Select) or tree.args.get("with_") or tree.args.get("with") or \
            any(isinstance(n, (exp.Subquery, exp.Union, exp.Intersect, exp.Except)) for n in tree.walk()) or \
            any(s is not tree for s in tree.find_all(exp.Select)):
        raise QueryRejected(f"{names}: totals only, in one plain SELECT (no WITH, subqueries or UNION)")
    for column in refs:
        parent = column.parent  # COUNT(DISTINCT x) would tell how many values repeat
        if not isinstance(parent, AGGREGATES) or isinstance(parent.parent, (exp.Window, exp.Filter)):
            raise QueryRejected(f"{column.name} is totals only: use it only as SUM({column.name}), "
                                f"AVG({column.name}) or COUNT({column.name}); single values aren't available")
    # some engines resolve HAVING names to the SELECT's aliases first, which would change what the threshold counts
    clash = sorted({a.alias for a in tree.find_all(exp.Alias)
                    if a.alias.lower() in reserved | {ROW_ID} | small_groups.RESERVED})
    if clash:
        raise QueryRejected(f"name the result column {clash[0]!r} something else: it is also the name of a table or of "
                            "a totals-only column")
    odd = next((n for n in [r.alias_or_name for r in checked.real_refs] + [c.table for c in tree.find_all(exp.Column)]
                if n and not PLAIN_NAME.fullmatch(n)), None)
    if odd:
        raise QueryRejected(f"{names}: give each table a plain alias (letters, digits and _) and qualify columns with "
                            f"it; {odd!r} isn't one")
    if tree.args.get("having"):  # an engine may evaluate it on the groups the threshold drops
        raise QueryRejected(f"{names}: HAVING isn't allowed with totals-only columns; leave it out and pick the groups "
                            "you need from the result")


def _with_threshold(checked: CheckedQuery, refs: list[exp.Column], totals: Mapping[str, frozenset[str]],
                    people: Mapping[str, frozenset[str]], min_group: int,
                    flagged: Mapping[str, frozenset[str]] | None = None
                    ) -> tuple[CheckedQuery, tuple[int, ...] | None]:
    """A copy of `checked` whose query keeps only groups of at least `min_group` different rows (or people, by each
    person column the table names) of each totals table (each row counted once, so a join can't weight one row) and
    `min_group` different values of each totals column. On tables with row rules, counted among each totals column's
    totals-only cells instead (`_gate`)."""
    flagged = flagged or {}
    tree = checked.tree.copy()
    copies = {id(old): new for old, new in zip(checked.tree.walk(), tree.walk(), strict=True)}
    real_refs = tuple(copies[id(r)] for r in checked.real_refs)
    at_least = exp.Literal.number(min_group)
    aliases = {r.alias_or_name.lower() for r in real_refs}
    conditions: list[exp.Expression] = [
        exp.GTE(this=exp.Count(this=exp.Distinct(expressions=[c.copy()])), expression=at_least.copy())
        for c in {c.sql(): c for c in refs}.values() if not _only_flagged(c, real_refs, totals, flagged, aliases)]
    for ref in real_refs:
        protected = totals.get(ref.name.lower())
        if not protected:
            continue
        row_ids = exp.column(ROW_ID, table=exp.to_identifier(ref.alias_or_name), quoted=True)
        counted = [exp.column(p, table=exp.to_identifier(ref.alias_or_name), quoted=True)
                   for p in sorted(people.get(ref.name.lower(), ()))] or [row_ids.copy()]
        flags = flagged.get(ref.name.lower(), frozenset())
        if flags:
            read = [c for c in sorted(protected) if _reads(ref, refs, frozenset({c}), aliases)]
            for column in read:
                conditions.append(_gate(ref, column, refs, counted, min_group) if column in flags else exp.and_(
                    *[exp.GTE(this=exp.Count(this=exp.Distinct(expressions=[c.copy()])), expression=at_least.copy())
                      for c in counted]))
        else:
            conditions += [exp.GTE(this=exp.Count(this=exp.Distinct(expressions=[c])), expression=at_least.copy())
                           for c in counted]
        if _reads(ref, refs, protected, aliases):
            conditions.append(exp.EQ(this=exp.Count(this=row_ids.copy()),
                                     expression=exp.Count(this=exp.Distinct(expressions=[row_ids.copy()]))))
    if tree.args.get("group"):
        merged = small_groups.merged(tree, conditions)
        return dataclasses.replace(checked, tree=merged.tree, real_refs=real_refs), merged.labels
    tree.set("having", exp.Having(this=exp.and_(*conditions)))
    return dataclasses.replace(checked, tree=tree, real_refs=real_refs), None


def _only_flagged(column: exp.Column, real_refs: Sequence[exp.Table], totals: Mapping[str, frozenset[str]],
                  flagged: Mapping[str, frozenset[str]], aliases: set[str]) -> bool:
    """Whether every table reference `column` could be a totals column of is flagged for it (row rules): its
    minimum is then counted among totals-only cells by `_gate`, not over every value."""
    name, qualifier = column.name.lower(), column.table.lower()
    owners = [r for r in real_refs if name in totals.get(r.name.lower(), frozenset())
              and (qualifier in ("", r.alias_or_name.lower()) or qualifier not in aliases)]
    return bool(owners) and all(name in flagged.get(r.name.lower(), frozenset()) for r in owners)


def _gate(ref: exp.Table, column: str, refs: Sequence[exp.Column], counted: Sequence[exp.Column],
          min_group: int) -> exp.Expression:
    """For one totals column of a table with row rules: the group holds none of its totals-only cells, or at least
    `min_group` different rows (people) and values among them. Full cells don't count."""
    alias = exp.to_identifier(ref.alias_or_name)
    flag = exp.column(exp.to_identifier(row_cells.flag_name(column), quoted=True), table=alias.copy())
    named = next(c for c in refs if c.name.lower() == column)
    value = exp.column(named.this.copy(), table=alias.copy())

    def among(expression: exp.Expression) -> exp.Expression:
        return exp.Case(ifs=[exp.If(this=exp.EQ(this=flag.copy(), expression=exp.Literal.number(1)),
                                    true=expression)])

    none = exp.EQ(this=exp.Count(this=among(exp.Literal.number(1))), expression=exp.Literal.number(0))
    enough = [exp.GTE(this=exp.Count(this=exp.Distinct(expressions=[among(c.copy())])),
                      expression=exp.Literal.number(min_group)) for c in (*counted, value)]
    return exp.Paren(this=exp.or_(none, exp.and_(*enough)))


def _reads(ref: exp.Table, refs: Sequence[exp.Column], protected: frozenset[str], aliases: set[str]) -> bool:
    """Whether a totals column of this table reference is summed: qualified by its alias, unqualified, or qualified
    by a name that is no alias in the query (each could mean this table)."""
    alias = ref.alias_or_name.lower()
    return any(c.name.lower() in protected and (c.table.lower() in ("", alias) or c.table.lower() not in aliases)
               for c in refs)


def row_id_tables(checked: CheckedQuery, totals: Mapping[str, frozenset[str]]) -> frozenset[str]:
    """The tables the rewrite gives a ROW_ID column, for the threshold to count."""
    return frozenset(t for t in checked.tables if totals.get(t))
