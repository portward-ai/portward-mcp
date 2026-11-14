"""Grants: what the applied profiles of one connection let the AI see (#91). Hide by default: a table or column no
applied profile grants is hidden, as if it didn't exist, including columns added after the grant.

`Grants` answers the same questions as rules.Rules (block rules, allow by default), so every path that hands source
data to the AI (pull.py, the catalog tools, shares, sync, exports) treats both alike: `store.rules_for(source)` gives
whichever the connection runs on, never both. `covering` turns the grants into the block rules they amount to on one
catalog (every table and column not granted), so the result re-check and SQLite's authorizer hold them too.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from portward_free import link_caps
from portward_free.context import Link
from portward_free.profiles import ANONYMISED, FULL, LEVELS, TOTALS, Grant, Key, hidden_why
from portward_free.row_rules import Cond as RowCond
from portward_free.row_rules import AllOf, RowRule, columns_of
from portward_free.rules import Rule, Rules
from portward_free.schema import Blocked
from portward_free.sources import Table
from portward_free.stand_ins import Scopes
from portward_free.totals import MIN_GROUP_SIZE

if TYPE_CHECKING:
    from portward_free.context import Meaning
    from portward_free.profiles import Profile

SOURCE = "profiles"  # the source name on the block rules `covering` makes; never stored


@dataclass(frozen=True)
class Grants:
    """The union of the applied profiles' grants on one connection: each column at the highest level granted. With
    the applied profiles' links, which say which anonymised columns share stand-ins (`stand_in_group`)."""

    levels: frozenset[tuple[Key, str]] = frozenset()
    scopes: Scopes = Scopes()
    people: frozenset[Key] = frozenset()  # (schema, table, person column), lower case, from the tables' meanings
    reach: frozenset[Reach] = frozenset()  # #132: per profile, on each table some applied profile has a row rule for
    # (table, column), lower case: linked columns at most anonymised, because a column of their link group is
    # anonymised or totals only somewhere (link_caps)
    capped: frozenset[tuple[str, str]] = frozenset()

    @classmethod
    def applied(cls, profiles: Iterable[Profile], elsewhere: Iterable[Profile] = ()) -> Grants:
        """What the applied `profiles` let the AI see together: their grants, links, person columns and row rules
        (store.rules_for, the profile editor's draft, why-hidden). A standing filter (#133) of any of them is joined
        with AND into every profile's reach on its table, so every read of that table keeps only its rows.
        `elsewhere`: the profiles applied on other connections; a column they anonymise lowers its link group here
        too (link_caps)."""
        profiles = tuple(profiles)
        found = cls.of((g for p in profiles for g in p.grants), (k for p in profiles for k in p.context.links),
                       meanings=[m for p in profiles for m in p.context.meanings])
        ruled = {r.table.lower() for p in profiles for r in p.rows}
        filters = {t: sorted({r.where for p in profiles for r in p.rows if r.standing and r.table.lower() == t},
                             key=repr) for t in ruled}
        reach = frozenset(Reach(table, _narrowed(rule.where if (rule := p.row_rule(table)) else None, filters[table]),
                                own)
                          for table in ruled for p in profiles
                          if (own := tuple(g for g in p.grants if g.table.lower() == table)))
        return replace(found, reach=reach, capped=link_caps.capped(link_caps.caps(profiles, elsewhere)))

    @classmethod
    def of(cls, grants: Iterable[Grant], links: Iterable[Link] = (), meanings: Iterable[Meaning] = ()) -> Grants:
        """`meanings` of the applied profiles name the person columns; two profiles naming different ones for the
        same table both count (each must reach the minimum)."""
        best: dict[Key, str] = {}
        for grant in grants:
            held = best.get(grant.key)
            if held is None or LEVELS.index(grant.level) < LEVELS.index(held):
                best[grant.key] = grant.level
        pairs = (((k.one.table.lower(), k.one.column.lower()), (k.other.table.lower(), k.other.column.lower()))
                 for k in links)
        people = frozenset(((m.schema or "").lower() or None, m.table.lower(), m.person.lower())
                           for m in meanings if m.person and m.column is None)
        return cls(frozenset(best.items()), Scopes.of(pairs), people)

    def stand_in_group(self, table: Table, column: str) -> str:
        """The link group whose stand-ins `column` of `table` shares (stand_ins.Scopes)."""
        return self.scopes.group(table.name, column)

    def persons(self, tables: Iterable[Table]) -> dict[str, frozenset[str]]:
        """The person columns named for each table (lower case), visible or not: totals on such a table count
        different values of each, not different rows (totals.py)."""
        found: dict[str, frozenset[str]] = {}
        for table in tables:
            schema = (table.schema or "").lower()
            named = frozenset(c for s, t, c in self.people if t == table.name.lower() and s in (None, schema))
            if named:
                found[table.name.lower()] = found.get(table.name.lower(), frozenset()) | named
        return found

    def level(self, table: Table, column: str) -> str | None:
        """The level `column` of `table` is granted at, or None (hidden)."""
        found = dict(self.levels)
        name, col = table.name.lower(), column.lower()
        keys = [(None, name, col)] + ([(table.schema.lower(), name, col)] if table.schema else [])
        held = [found[k] for k in keys if k in found]
        return min(held, key=LEVELS.index) if held else None

    def visible(self, tables: Iterable[Table]) -> tuple[Table, ...]:
        """The catalog as the AI may see it: granted columns this connection can apply as granted, of tables that
        aren't derived (a view could read a hidden column under another name), and nothing else. A grant it can't
        apply (totals on free text) falls to hidden, never to full."""
        shown = []
        for table in tables:
            if table.derived:
                continue
            columns = tuple(c for c in table.columns if self.shape(table, c))
            if columns:
                numeric = frozenset(c for c in table.numeric if c in columns)
                shown.append(Table(table.name, columns, schema=table.schema, numeric=numeric))
        return tuple(shown)

    def shape(self, table: Table, column: str) -> str | None:
        """How the AI may use `column` of `table`: the level it is shown at, or None (hidden). On a table without row
        rules, the level granted when this connection can apply it. With row rules, from its cells (`cells`): totals
        when any cell is totals only (it may then only be totalled, and its anonymised cells count as totals), else
        anonymised when any cell may be a stand-in, else full."""
        found = self.cells(table)
        if found is not None:
            cells = found.columns.get(column.lower())
            return cells.shape if cells else None
        level = self.level(table, column)
        if level == FULL and self.is_capped(table, column):
            level = ANONYMISED
        return level if level is not None and hidden_why(level, table, column) is None else None

    def is_capped(self, table: Table, column: str) -> bool:
        """Whether `column` of `table` is at most anonymised because of its link group (link_caps)."""
        return (table.name.lower(), column.lower()) in self.capped

    def cells(self, table: Table) -> Cells | None:
        """Per-cell levels of `table` when an applied profile has a row rule for it (None otherwise): each cell takes
        the highest level among the profiles whose row rule matches its row (a profile without a rule for the table
        reaches every row), so a profile never lends its level to rows it doesn't reach. Rules are matched by table
        name only (a rule naming a schema also covers a same-named table elsewhere: narrower, never wider)."""
        reaching = [r for r in self.reach if r.table == table.name.lower()]
        if not reaching or table.derived:
            return None
        columns: dict[str, ColumnCells] = {}
        rows: list[RowCond | None] = []
        for column in table.columns:
            by_level: dict[str, list[RowCond | None]] = {FULL: [], ANONYMISED: [], TOTALS: []}
            for reach in sorted(reaching, key=repr):
                level = Grants.of(reach.grants).level(table, column)
                if level is not None and hidden_why(level, table, column) is None:
                    by_level[level].append(reach.where)
            if self.is_capped(table, column):  # full cells go to stand-ins, or the column to hidden
                by_level = {FULL: [], ANONYMISED: by_level[FULL] + by_level[ANONYMISED], TOTALS: by_level[TOTALS]}
                if hidden_why(ANONYMISED, table, column) is not None:
                    by_level = {**by_level, ANONYMISED: []}
            if any(by_level.values()):
                columns[column.lower()] = ColumnCells.of(by_level)
                rows += [w for found in by_level.values() for w in found]
        return Cells(table.columns, _either(rows), tuple(sorted(columns.items())))

    def rule_columns(self, tables: Iterable[Table]) -> frozenset[tuple[str, str]]:
        """(table, column), lower case, of the columns row rules name on these tables: Portward's own rewrite reads
        them (row_cells.py) even when they are hidden, so the engine-level block lets those reads through. The AI
        still can't name them: the swapped-in table has no such column."""
        return frozenset((t.name.lower(), c.lower()) for t in tables for r in self.reach
                         if r.table == t.name.lower() and r.where is not None for c in columns_of(r.where))

    def totals(self, tables: Iterable[Table]) -> dict[str, frozenset[str]]:
        """The visible columns granted as totals only, by table (lower case): read only through SUM, AVG and COUNT,
        in groups of at least the threshold (totals.py)."""
        found = {t.name.lower(): frozenset(c.lower() for c in t.columns if self.shape(t, c) == TOTALS)
                 for t in self.visible(tables)}
        return {t: cols for t, cols in found.items() if cols}

    def fallen(self, tables: Iterable[Table]) -> tuple[tuple[str, str], ...]:
        """Granted columns of non-derived tables this connection can't apply as granted, hidden instead: (name, why)."""
        found = []
        for table in tables:
            for column in () if table.derived else table.columns:
                level = self.level(table, column)
                why = hidden_why(level, table, column) if level else None
                if why:
                    found.append((f"{table.name}.{column}", why))
        return tuple(found)

    def covering(self, tables: Iterable[Table]) -> Rules:
        """The block rules these grants amount to on this catalog: every table with nothing visible, and every
        column not visible in the others."""
        tables = tuple(tables)
        visible = {t.name.lower(): {c.lower() for c in t.columns} for t in self.visible(tables)}
        found: list[Rule] = []
        for table in tables:
            shown = visible.get(table.name.lower())
            if shown is None:
                found.append(Rule(SOURCE, table.name))
                continue
            found += [Rule(SOURCE, table.name, c) for c in table.columns if c.lower() not in shown]
        return Rules(tuple(dict.fromkeys(found)))

    def notices(self, tables: Iterable[Table]) -> tuple[str, ...]:
        """What the AI should know about how it may use what it sees: the columns it gets as stand-ins and the
        columns it may only total. Never what is hidden, or why (spec story 20): granted views and grants that fell
        to hidden are for the user, on the why-hidden page."""
        tables = tuple(tables)
        said: list[str] = []
        persons = self.persons(tables)
        said += [f"{table}.{column} is totals only: use it only in SUM, AVG or COUNT; groups of fewer than "
                 f"{MIN_GROUP_SIZE} {_counted(persons.get(table))} are merged into one 'other (small groups)' row"
                 for table, cols in sorted(self.totals(tables).items()) for column in sorted(cols)]
        if anonymised := self.anonymised(tables):
            said.append(f"{', '.join(anonymised)} {'is' if len(anonymised) == 1 else 'are'} anonymised: you get a "
                        "stand-in for each value, the same value always the same stand-in within a column and "
                        "across columns a link joins (only there), so filter, group and join on them; a real value "
                        "never matches one, and the user sees the real values")
        return tuple(said)

    def anonymised(self, tables: Iterable[Table]) -> tuple[str, ...]:
        """`table.column` of each visible column granted anonymised."""
        return tuple(f"{t.name}.{c}" for t in self.visible(tables) for c in t.columns
                     if self.shape(t, c) == ANONYMISED)

    def withheld_derived(self, tables: Iterable[Table]) -> tuple[str, ...]:
        return tuple(t.name for t in tables if t.derived and any(self.level(t, c) for c in t.columns))

    def explain_refusal(self, tables: Sequence[str], referenced: Sequence[str]) -> None:
        """Nothing to explain: a hidden column doesn't exist as far as the AI knows."""

    def browse(self, tables: Iterable[Table]) -> tuple[tuple[Table, ...], Blocked]:
        """What describe_source shows: only what is granted, and nothing marked hidden (hidden doesn't exist)."""
        return self.visible(tables), frozenset()

    def payload(self) -> dict[str, list]:
        """A JSON form, for held pulls and selections (rules.policy_from reads it back)."""
        found: dict[str, list] = {"grants": sorted([[s, t, c, level] for (s, t, c), level in self.levels],
                                                   key=lambda g: tuple(x or "" for x in g))}
        if self.scopes.links:
            found["links"] = sorted([list(one), list(other)] for one, other in self.scopes.links)
        if self.people:
            found["people"] = sorted([list(p) for p in self.people], key=lambda p: tuple(x or "" for x in p))
        if self.reach:
            found["reach"] = sorted((r.payload() for r in self.reach), key=repr)
        if self.capped:
            found["capped"] = sorted([list(c) for c in self.capped])
        return found

    @classmethod
    def from_payload(cls, data: dict[str, list]) -> Grants:
        pairs = ((tuple(map(str, one)), tuple(map(str, other))) for one, other in data.get("links", ()))
        people = frozenset((s, str(t), str(c)) for s, t, c in data.get("people", ()))
        reach = frozenset(Reach.from_payload(r) for r in data.get("reach", ()))
        capped = frozenset((str(t), str(c)) for t, c in data.get("capped", ()))
        return cls(frozenset(((s, str(t), str(c)), str(level)) for s, t, c, level in data["grants"]),
                   Scopes.of(pairs), people, reach, capped)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Reach:
    """One applied profile on a table some applied profile has a row rule for: the rows it reaches (`where`; None:
    every row) and its grants on that table."""

    table: str  # lower case
    where: RowCond | None
    grants: tuple[Grant, ...]

    def payload(self) -> dict[str, object]:
        return {"table": self.table,
                "where": RowRule(self.table, self.where).stored()["where"] if self.where is not None else None,
                "grants": [[g.schema, g.table, g.column, g.level] for g in self.grants]}

    @classmethod
    def from_payload(cls, data: dict) -> Reach:
        where = data.get("where")
        rule = RowRule.from_stored({"table": data["table"], "where": where}) if where is not None else None
        return cls(str(data["table"]).lower(), rule.where if rule else None,
                   tuple(Grant(str(t), str(c), s, str(lv)) for s, t, c, lv in data["grants"]))


def _narrowed(where: RowCond | None, filters: Sequence[RowCond]) -> RowCond | None:
    """`where` (None: every row) and every standing filter, all at once."""
    parts = [*filters, *([where] if where is not None else [])]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else AllOf(tuple(parts))


Rows = tuple[RowCond, ...] | None  # the rows matching any of these rules; None: every row; (): no row


def _either(wheres: Iterable[RowCond | None]) -> Rows:
    found: list[RowCond] = []
    for where in wheres:
        if where is None:
            return None
        if where not in found:
            found.append(where)
    return tuple(found)


@dataclass(frozen=True)
class ColumnCells:
    """Where one column's cells are at each level or higher: `full` rows show it in full, `anonymised` rows at least
    as a stand-in, `any` rows at least as totals (outside `any` the cell is empty). `shape` is Grants.shape."""

    full: Rows
    anonymised: Rows
    any: Rows
    shape: str

    @classmethod
    def of(cls, by_level: dict[str, list[RowCond | None]]) -> ColumnCells:
        full = _either(by_level[FULL])
        anonymised = _either(by_level[FULL] + by_level[ANONYMISED])
        reached = _either(by_level[FULL] + by_level[ANONYMISED] + by_level[TOTALS])
        if full is None:
            shape = FULL
        elif by_level[TOTALS] and anonymised is not None:
            shape = TOTALS
        elif by_level[ANONYMISED]:
            shape = ANONYMISED
        else:
            shape = FULL
        return cls(full, anonymised, reached, shape)


@dataclass(frozen=True)
class Cells:
    """A table's cells under row rules: the `rows` the AI may see at all (a row no profile reaches doesn't exist for
    it) and each column's cells, for the columns some reaching profile grants. `real` names every column of the
    table, so a rule can name a hidden one."""

    real: tuple[str, ...]
    rows: Rows
    columns_by_name: tuple[tuple[str, ColumnCells], ...] = ()

    @property
    def columns(self) -> dict[str, ColumnCells]:
        return dict(self.columns_by_name)


def _counted(persons: frozenset[str] | None) -> str:
    return f"people (by {', '.join(sorted(persons))})" if persons else "rows"
