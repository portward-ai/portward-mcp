"""A source's tables and columns, with the user's notes and which ones are blocked.

`describe` is what the user browses. Blocking comes from #39's block rules: pass them in as `blocked`, a set of
`(table, None)` for a whole table and `(table, column)` for one column. Until those rules exist nothing is blocked.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from portward_free.sources import Table

TABLE_NOTE = ""  # the column name a table's own note is stored under

Notes = Mapping[tuple[str, str], str]  # (table, column or TABLE_NOTE) -> note
Blocked = frozenset[tuple[str, str | None]]
Details = Mapping[tuple[str, str], Mapping[str, Any]]  # (table, column or TABLE_NOTE) -> synonyms/values/samples

NOTHING_BLOCKED: Blocked = frozenset()


def describe(tables: Iterable[Table], notes: Notes, blocked: Blocked = NOTHING_BLOCKED,
             details: Details | None = None) -> list[dict[str, Any]]:
    """Blocked tables and columns are listed and marked, so the user sees what is protected, but their notes are
    left out: this reaches the AI too, and a note can say what a blocked column holds. `details` (#131, profiles
    only, where nothing is blocked) adds synonyms, value meanings and sample values."""
    details = details or {}
    described = []
    for table in tables:
        table_blocked = (table.name, None) in blocked
        columns = []
        for column in table.columns:
            column_blocked = table_blocked or (table.name, column) in blocked
            extra = {} if column_blocked else details.get((table.name, column), {})
            columns.append({"name": column, "note": None if column_blocked else notes.get((table.name, column)),
                            "blocked": column_blocked} | extra)
        described.append({
            "name": table.name,
            "note": None if table_blocked else notes.get((table.name, TABLE_NOTE)),
            "blocked": table_blocked,
            "columns": columns,
        } | ({} if table_blocked else details.get((table.name, TABLE_NOTE), {})))
    return described


def for_ai(tables: Iterable[Table], notes: Notes, details: Details | None = None) -> list[dict[str, Any]]:
    """What the AI reads before it writes a query: tables, columns and, where the user wrote them, notes. With a
    profile's meanings (#131), their synonyms, value meanings and sample values (`details`)."""
    details = details or {}
    listed = []
    for table in tables:
        entry: dict[str, Any] = {"name": table.name, "columns": list(table.columns)}
        if (table.name, TABLE_NOTE) in notes:
            entry["note"] = notes[(table.name, TABLE_NOTE)]
        if synonyms := details.get((table.name, TABLE_NOTE), {}).get("synonyms"):
            entry["synonyms"] = synonyms
        column_notes = {c: notes[(table.name, c)] for c in table.columns if (table.name, c) in notes}
        if column_notes:
            entry["column_notes"] = column_notes
        for key, field in (("column_synonyms", "synonyms"), ("column_values", "values"),
                           ("column_samples", "sample_values")):
            found = {c: details[(table.name, c)][field] for c in table.columns
                     if field in details.get((table.name, c), {})}
            if found:
                entry[key] = found
        listed.append(entry)
    return listed
