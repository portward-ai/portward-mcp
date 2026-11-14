"""The results panel's column menu (#119): which table column a result column is, and what the user can do with it
there: keep it from Claude (today's block rule, renamed in the UI) and write its note.

A result column can be an alias or worked out from several columns, so the panel never names a table column itself:
the server traces the pull's own SQL (provenance) and acts only on a column read from exactly one table column. On a
connection on profiles block rules and Free's notes don't apply, so the menu says where to change things instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlglot.errors import SqlglotError

from portward_free import provenance
from portward_free.results_panel import Held
from portward_free.rule_tools import Opener, named_rule
from portward_free.rules import Rule
from portward_free.schema import TABLE_NOTE
from portward_free.sources import SourceError
from portward_free.storage import LocalStore

ON_PROFILES = ("{source} runs on profiles, so what the AI gets is changed in the profile editor on the Portward "
               "page, not here")
GONE = "{source} isn't connected any more"


class MenuError(ValueError):
    """The column can't be kept or noted from the panel; the message says why, for the user."""


@dataclass(frozen=True)
class Target:
    """The table column a result column reads, named as the source names it."""

    source: str
    table: str
    column: str

    def rule(self) -> Rule:
        return Rule(self.source, self.table, self.column)

    def payload(self) -> dict[str, str]:
        return {"source": self.source, "table": self.table, "column": self.column}


def target_of(store: LocalStore, opened: Opener, held: Held, column: str) -> Target:
    """The one table column behind result column `column` of a held pull; MenuError when there isn't exactly one,
    or the source can't take block rules and notes (profiles)."""
    if column not in held.columns:
        raise MenuError(f"no column {column!r} in this result")
    connection = store.connection(held.source)
    if connection is None:
        raise MenuError(GONE.format(source=held.source))
    if store.profiles.on_profiles(connection.name):
        raise MenuError(ON_PROFILES.format(source=connection.name))
    unsure = f"Portward couldn't tell which column {column} is: {{}}"
    try:
        with opened(connection) as source:
            catalog = source.tables()
            traced = provenance.trace(held.sql, source.dialect, column, {t.name: t.columns for t in catalog})
    except (SourceError, SqlglotError, ValueError) as err:
        raise MenuError(unsure.format(err)) from None
    read = sorted({(o.table, o.column) for o in traced.origins if o.column is not None})
    if not traced.traced or len(read) != 1:
        raise MenuError(_not_one(column, traced))
    try:
        rule = named_rule(connection.name, catalog, read[0][0], read[0][1])
    except ValueError as err:  # a table or column the query names but the source no longer has
        raise MenuError(unsure.format(err)) from None
    assert rule.column is not None
    return Target(connection.name, rule.table, rule.column)


def _not_one(column: str, traced: provenance.Provenance) -> str:
    named = sorted({f"{o.table}.{o.column}" if o.column else o.table for o in traced.origins})
    return (f"{column} is worked out from {', '.join(named)}, not read from one column; keep or note those on the "
            "Portward page")


def is_kept(store: LocalStore, target: Target) -> bool:
    """Kept from the AI: the column, or its whole table, has a block rule."""
    return any(r.table.lower() == target.table.lower()
               and (r.column is None or r.column.lower() == target.column.lower())
               for r in store.rules(target.source))


def menu(store: LocalStore, opened: Opener, held: Held, column: str) -> dict[str, Any]:
    """What the column menu shows: the table column, whether it is kept, its note; or why it can't change here."""
    try:
        target = target_of(store, opened, held, column)
    except MenuError as err:
        return {"column": column, "target": None, "kept": column in _kept_here(held), "note": None,
                "why": str(err)}
    note = store.notes(target.source).get((target.table, target.column))
    return {"column": column, "target": target.payload(), "kept": is_kept(store, target), "note": note, "why": None}


def _kept_here(held: Held) -> tuple[str, ...]:
    return held.receipt.kept_columns if held.receipt is not None else ()


def keep(store: LocalStore, opened: Opener, held: Held, column: str) -> tuple[Target, bool]:
    """Keep the column from the AI from the next result on. Only narrows, so it needs no confirm."""
    target = target_of(store, opened, held, column)
    return target, store.add_rule(target.rule())


MAX_NOTE = 2_000  # characters; a note travels with the schema in every describe


def write_note(store: LocalStore, opened: Opener, held: Held, column: str, note: str) -> tuple[Target, str | None]:
    """Write, replace or (empty) remove the column's note; it reaches the AI with the schema, as notes do."""
    if len(note) > MAX_NOTE:
        raise MenuError(f"a note can be at most {MAX_NOTE} characters")
    target = target_of(store, opened, held, column)
    assert target.column != TABLE_NOTE
    store.set_note(target.source, target.table, target.column, note)
    return target, note.strip() or None
