"""What row rules do to a connection, in words, for the user only (#132): which rows of a table reach the AI, which
columns it gets only on some rows, and where one applied profile lowers what another shows (spec story 36). The
profile editor's preview shows these and why-hidden lists them. Never AI-facing: the AI isn't told rows exist that
it can't see.

Lowering comes from two rules of combining per cell (grants.Grants.cells):
- a value anonymised on any row is anonymised on every row, so a profile anonymising a column on its rows turns the
  same values into stand-ins on rows another profile shows in full;
- a column with any totals-only cell may only be totalled, so a profile granting totals on its rows makes another
  profile's full (or anonymised) cells of that column usable only in totals.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from portward_free.grants import Grants
from portward_free.link_caps import LinkCap
from portward_free.profiles import ANONYMISED, FULL, TOTALS, Profile, hidden_why
from portward_free.sources import Table

EVERY_ROW = "every row"


@dataclass(frozen=True)
class Rows:
    """The rows of `table` the AI can see at all, through which profiles, and the standing filters (#133) every read
    of it keeps to."""

    table: str
    reached: tuple[tuple[str, str], ...]  # (profile name, its rows: a rule's text or EVERY_ROW)
    filters: tuple[tuple[str, str], ...] = ()  # (profile name, its standing filter's text)

    @property
    def profiles(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys([n for n, _ in self.reached] + [n for n, _ in self.filters]))

    @property
    def text(self) -> str:
        said = []
        if any(rows != EVERY_ROW for _, rows in self.reached):
            said.append("only rows where " + "; ".join(f"{rows} (from {name!r})" for name, rows in self.reached)
                        + " reach the AI")
        if self.filters:
            said.append(f"every read of {self.table} keeps only rows where " + "; ".join(
                f"{rows} (standing filter from {name!r})" for name, rows in self.filters))
        return (f"{', and '.join(said)}; every other row of {self.table} is hidden, and the AI isn't told it "
                "exists")


@dataclass(frozen=True)
class SomeRows:
    """A column the AI gets only on some rows of its table (empty on the others)."""

    table: str
    column: str
    reached: tuple[tuple[str, str, str], ...]  # (profile name, level, its rows)

    @property
    def text(self) -> str:
        said = "; ".join(f"{name!r} grants it as {level} on {_on(rows)}" for name, level, rows in self.reached)
        return f"{said}; on other rows the AI gets {self.table}.{self.column} empty"


@dataclass(frozen=True)
class Lowered:
    """One applied profile lowering what another shows of a column, on the other's rows."""

    table: str
    column: str
    of: str  # the profile whose cells are lowered
    of_rows: str
    was: str
    by: str  # the profile that lowers them
    by_rows: str
    now: str

    @property
    def name(self) -> str:
        return f"{self.table}.{self.column}"

    @property
    def text(self) -> str:
        if self.now == ANONYMISED:
            return (f"{self.name}: {self.of!r} shows it in full on {_on(self.of_rows)}, but {self.by!r} anonymises it "
                    f"on {_on(self.by_rows)}. A value anonymised on one row is anonymised on every row, so on "
                    f"{self.of!r}'s rows the values that also appear on {self.by!r}'s rows are stand-ins too.")
        return (f"{self.name}: {self.of!r} shows it {self.was} on {_on(self.of_rows)}, but {self.by!r} makes it "
                f"totals only on {_on(self.by_rows)}. A column with totals-only cells can only be totalled, so on "
                f"{self.of!r}'s rows the AI can only total it too.")


def _on(rows: str) -> str:
    return EVERY_ROW if rows == EVERY_ROW else f"rows where {rows}"


@dataclass(frozen=True)
class RowReasons:
    rows: tuple[Rows, ...] = ()
    some_rows: tuple[SomeRows, ...] = ()
    lowered: tuple[Lowered, ...] = ()
    linked: tuple[LinkCap, ...] = ()  # full columns anonymised because of their link group (link_caps)

    def __bool__(self) -> bool:
        return bool(self.rows or self.some_rows or self.lowered or self.linked)


def _rows_of(profile: Profile, table: Table) -> str:
    rule = profile.row_rule(table.name)
    return rule.text if rule else EVERY_ROW


def _levels(profile: Profile, table: Table, column: str) -> str | None:
    level = Grants.of(profile.grants).level(table, column)
    return level if level is not None and hidden_why(level, table, column) is None else None


def row_reasons(profiles: Sequence[Profile], catalog: Iterable[Table]) -> RowReasons:
    """What the row rules of the applied `profiles` do to each table of `catalog`."""
    grants = Grants.applied(profiles)
    rows: list[Rows] = []
    some: list[SomeRows] = []
    lowered: list[Lowered] = []
    for table in catalog:
        cells = grants.cells(table)
        if cells is None:
            continue
        granting = [p for p in profiles if any(_levels(p, table, c) for c in table.columns)]
        if cells.rows is not None and granting:
            filters = tuple((p.name, f.text) for p in profiles if (f := p.standing_filter(table.name)))
            rows.append(Rows(table.name, tuple((p.name, _rows_of(p, table)) for p in granting), filters))
        for column in table.columns:
            found = cells.columns.get(column.lower())
            if found is None:
                continue
            levels = [(p, lv) for p in profiles if (lv := _levels(p, table, column))]
            if found.any is not None and found.any != cells.rows:
                some.append(SomeRows(table.name, column, tuple((p.name, lv, _rows_of(p, table)) for p, lv in levels)))
            lowered += _lowered(table, column, found.shape, found.full, levels)
    return RowReasons(tuple(rows), tuple(some), tuple(lowered))


def _lowered(table: Table, column: str, shape: str, full: object, levels: list) -> list[Lowered]:
    found = []
    if shape == ANONYMISED and full is not None:
        for of, was in levels:
            for by, now in levels:
                if was == FULL and now == ANONYMISED and of is not by:
                    found.append(Lowered(table.name, column, of.name, _rows_of(of, table), was, by.name,
                                         _rows_of(by, table), ANONYMISED))
    if shape == TOTALS:
        for of, was in levels:
            for by, now in levels:
                if was in (FULL, ANONYMISED) and now == TOTALS and of is not by:
                    found.append(Lowered(table.name, column, of.name, _rows_of(of, table), was, by.name,
                                         _rows_of(by, table), TOTALS))
    return found
