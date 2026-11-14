"""Context drift (#123, Personal): context that names a column its table no longer has.

When a column is renamed or dropped, a meaning, definition or link written about it would quietly mislead the AI. On
connect, every context item of the profiles applied to a connection is checked against the connection's catalog:
- a meaning of a column its table no longer has, or a link with an end like that;
- a text naming `table.column` (or `schema.table.column`) where the table is here but the column isn't;
- a text using a bare word that is a column the profile grants, of a table here that no longer has it, and that
  isn't a table or column anywhere in this connection.
Such an item is held back from the AI (context_served drops it) and listed for the user on the local Portward page
with a suggested fix (why_hidden, `Kind.COLUMN_GONE`). The weekly facts carry only how many per connection
(weekly_facts.remember_held_back), never the item: the control plane can't read context.

A table missing from this connection isn't drift here: a profile applied to several connections grants tables of
each, so only a column missing from a table that is here counts. The check is worked out afresh each time, so
rewording the item (or the column coming back) releases it at once.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from portward_free.context import End, mentions
from portward_free.sources import Table
from portward_free.verified_def import VerifiedAnswer, VerifiedError, reads

if TYPE_CHECKING:
    from portward_free.profiles import Grant, Profile
    from portward_free.storage import LocalStore

CLOSE_ENOUGH = 0.6  # difflib ratio for "it was most likely renamed to this"
COLUMNS_LISTED = 6  # current columns named in a suggestion when none is close


@dataclass(frozen=True)
class Gone:
    """A column a context item names that its table no longer has. `maybe` is the table's current column it was
    most likely renamed to (None when none is close); `others` the table's columns, for the suggestion."""

    table: str
    column: str
    maybe: str | None = None
    others: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return f"{self.table}.{self.column}"

    def fix(self) -> str:
        if self.maybe:
            return (f"if it was renamed to {self.maybe}, reword it to name {self.table}.{self.maybe} (and grant that "
                    f"column if the AI should see it)")
        listed = ", ".join(self.others[:COLUMNS_LISTED]) + (", ..." if len(self.others) > COLUMNS_LISTED else "")
        return f"reword it to name a column {self.table} has now ({listed or 'none'}), or remove it"


@dataclass(frozen=True)
class Drifted:
    """One context item held back because it names a column that's gone. `about` is as why_hidden lists it."""

    about: str  # "meaning of orders.total", "definition revenue", "link a.b = c.d"
    what: str  # meaning, definition, link, metric or verified answer
    profile: str  # the profile's name
    gone: tuple[Gone, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(g.name for g in self.gone)

    @property
    def why(self) -> str:
        which = ", ".join(self.names)
        tables = ", ".join(dict.fromkeys(g.table for g in self.gone))
        return (f"{self.about} (in {self.profile!r}) names {which}, which {tables} no longer "
                f"{'has' if len(self.gone) == 1 else 'have'}, so the AI isn't told it; "
                + "; ".join(g.fix() for g in self.gone))


def _tables(catalog: Sequence[Table]) -> dict[str, Table]:
    return {t.name.lower(): t for t in catalog}


def _gone(table: Table, column: str) -> Gone:
    columns = {c.lower(): c for c in table.columns}
    close = difflib.get_close_matches(column.lower(), list(columns), n=1, cutoff=CLOSE_ENOUGH)
    return Gone(table.name, column, columns[close[0]] if close else None, tuple(table.columns))


def _missing(tables: dict[str, Table], table: str, column: str | None) -> Gone | None:
    """The column as Gone when `table` is here and doesn't have it; None otherwise."""
    found = tables.get(table.lower())
    if found is None or column is None or column.lower() in {c.lower() for c in found.columns}:
        return None
    return _gone(found, column)


def gone_in(text: str, grants: Sequence[Grant], catalog: Sequence[Table]) -> tuple[Gone, ...]:
    """The columns `text` names that their table here no longer has (see the module docstring for how)."""
    tables = _tables(catalog)
    every = set(tables) | {c.lower() for t in catalog for c in t.columns}
    found: dict[str, Gone] = {}
    for name in mentions(text):
        parts = name.split(".")
        if len(parts) > 1:
            candidates = [_missing(tables, parts[-2], parts[-1])]
        elif parts[0].lower() in every:
            continue
        else:
            candidates = [_missing(tables, g.table, g.column) for g in grants if g.column.lower() == parts[0].lower()]
        for gone in (g for g in candidates if g is not None):
            found.setdefault(gone.name.lower(), gone)
    return tuple(found.values())


def _merged(*groups: Iterable[Gone | None]) -> tuple[Gone, ...]:
    found: dict[str, Gone] = {}
    for gone in (g for group in groups for g in group if g is not None):
        found.setdefault(gone.name.lower(), gone)
    return tuple(found.values())


def _end(tables: dict[str, Table], end: End) -> Gone | None:
    return _missing(tables, end.table, end.column)


def answer_gone(answer: VerifiedAnswer, grants: Sequence[Grant], catalog: Sequence[Table]) -> tuple[Gone, ...]:
    """The columns a verified answer's SQL or question names that their table here no longer has (#130), including
    columns named through a table alias (o.status)."""
    tables = _tables(catalog)
    try:
        read = reads(answer.sql, answer.dialect)
    except VerifiedError:
        read = None
    qualified = [] if read is None else [
        _missing(tables, read.tables[q.lower()], c) for q, c in read.columns if q and q.lower() in read.tables]
    return _merged(qualified, gone_in(answer.sql, grants, catalog), gone_in(answer.question, grants, catalog))


def drifted(profiles: Sequence[Profile], catalog: Sequence[Table],
            source: str | None = None) -> tuple[Drifted, ...]:
    """Every context item of `profiles` that names a column gone from `catalog`. With `source`, verified answers of
    other connections are left out (#130: one belongs to the connection it was verified on)."""
    tables = _tables(catalog)
    found: list[Drifted] = []
    for profile in profiles:
        for meaning in profile.context.meanings:
            subject = f"{meaning.table}.{meaning.column}" if meaning.column else meaning.table
            if gone := _merged([_missing(tables, meaning.table, meaning.column)],
                               gone_in(meaning.words, profile.grants, catalog)):
                found.append(Drifted(f"meaning of {subject}", "meaning", profile.name, gone))
        for definition in profile.context.definitions:
            if gone := gone_in(definition.text, profile.grants, catalog):
                found.append(Drifted(f"definition {definition.term}", "definition", profile.name, gone))
        for link in profile.context.links:
            if gone := _merged([_end(tables, link.one), _end(tables, link.other)],
                               gone_in(link.text or "", profile.grants, catalog)):
                found.append(Drifted(f"link {link.one.name} = {link.other.name}", "link", profile.name, gone))
        for metric in profile.context.metrics:  # #129
            if gone := _merged([_missing(tables, metric.table, c) for c in metric.columns],
                               gone_in(metric.text, profile.grants, catalog)):
                found.append(Drifted(f"metric {metric.name}", "metric", profile.name, gone))
        for answer in profile.context.verified:  # #130
            if source is not None and answer.source.lower() != source.lower():
                continue
            if gone := answer_gone(answer, profile.grants, catalog):
                found.append(Drifted(f"verified answer {answer.question}", "verified answer", profile.name, gone))
    return tuple(found)


def drifted_on(store: LocalStore, source: str, catalog: Sequence[Table]) -> tuple[Drifted, ...]:
    """The held-back items on `source` (none on a connection running on block rules)."""
    if not store.profiles.on_profiles(source):
        return ()
    return drifted([a.profile for a in store.profiles.applied(source)], catalog, source)
