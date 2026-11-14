"""A starting profile from Free's block rules and notes (#92): when a Free user upgrades, each connection still on
block rules can become a profile that grants, in full and by name, exactly the columns the AI sees there now, with the
user's notes on those tables and columns carried over as the profile's context. Notes on anything blocked are dropped:
context never speaks about what a profile hides."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from portward_free.profiles import Grant, Meaning, about_granted
from portward_free.rules import Rules
from portward_free.sources import Table

TABLE_NOTE = ""  # a note with no column is the table's own (storage: notes.column_name '')


def starting_grants(rules: Rules, catalog: Iterable[Table]) -> tuple[Grant, ...]:
    """Every column not blocked, by name, granted in full: what `rules` let the AI see of `catalog`."""
    return tuple(Grant(t.name, c) for t in rules.visible(catalog) for c in t.columns)


def carried_notes(notes: Mapping[tuple[str, str], str], grants: Iterable[Grant]) -> tuple[Meaning, ...]:
    """The notes on what `grants` grant, as meanings named the way the data names them; the rest are dropped."""
    grants = tuple(grants)
    spelled = {g.key[1:]: (g.table, g.column) for g in grants}
    tables = {g.table.lower(): g.table for g in grants}
    meanings = []
    for (table, column), text in sorted(notes.items(), key=lambda n: (n[0][0].lower(), n[0][1].lower())):
        if column == TABLE_NOTE:
            meanings.append(Meaning(tables.get(table.lower(), table), None, text))
        else:
            named_table, named_column = spelled.get((table.lower(), column.lower()), (table, column))
            meanings.append(Meaning(named_table, named_column, text))
    return about_granted(meanings, grants)
