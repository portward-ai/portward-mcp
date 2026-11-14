"""Block rules: the tables and columns of a source the user keeps from the AI.

A rule names a table (the whole table is blocked) or a table and a column. Names match case-insensitively, the way
SQL identifiers do. Everything not blocked is allowed.

Every path that hands source data to the AI, or out of the machine (shares, sync, exports), must go through
`Rules.visible` and `Rules.check_result`; see pull.py for the query path. A connection on profiles (Personal) gets
grants.Grants from `store.rules_for` instead, which answers the same questions with hide by default (`Policy`).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from portward_free.guard import QueryRejected
from portward_free.sources import Table

SHOWN_IN_FULL = "full"  # profiles.FULL: what block rules let through is shown as it is


class Policy(Protocol):
    """What the AI may see of one connection: block rules (Rules) or the applied profiles' grants (grants.Grants)."""

    def visible(self, tables: Iterable[Table]) -> tuple[Table, ...]: ...

    def level(self, table: Table, column: str) -> str | None: ...

    def shape(self, table: Table, column: str) -> str | None: ...

    def covering(self, tables: Iterable[Table]) -> Rules: ...

    def notices(self, tables: Iterable[Table]) -> tuple[str, ...]: ...

    def withheld_derived(self, tables: Iterable[Table]) -> tuple[str, ...]: ...

    def explain_refusal(self, tables: Sequence[str], referenced: Sequence[str]) -> None: ...

    def browse(self, tables: Iterable[Table]) -> tuple[tuple[Table, ...], frozenset[tuple[str, str | None]]]: ...

    def payload(self) -> Any: ...


@dataclass(frozen=True)
class Rule:
    source: str
    table: str
    column: str | None = None  # None blocks the whole table

    def describe(self) -> str:
        return f"{self.table}.{self.column}" if self.column else f"table {self.table}"


@dataclass(frozen=True)
class Rules:
    """The block rules of one source."""

    rules: tuple[Rule, ...] = ()

    @property
    def blocked_tables(self) -> frozenset[str]:
        return frozenset(r.table.lower() for r in self.rules if r.column is None)

    @property
    def blocked_columns(self) -> frozenset[tuple[str, str]]:
        """(table, column) pairs, lower case."""
        return frozenset((r.table.lower(), r.column.lower()) for r in self.rules if r.column is not None)

    @property
    def blocked(self) -> frozenset[tuple[str, str | None]]:
        """The rules as schema.describe takes them: (table, None) for a whole table, (table, column) for a column,
        named as the source names them."""
        return frozenset((r.table, r.column) for r in self.rules)

    def covering(self, tables: Iterable[Table]) -> Rules:
        """These rules as they hold on this catalog: a rule on a partition or inheritance child also holds on every
        table whose rows include it (its parents, up the chain), and a table with a column rule naming a column it
        no longer has (renamed, most likely) is blocked whole until the user re-blocks the new name or removes the
        rule. Names can't follow a rename (only Postgres has stable column numbers), so this fails closed."""
        catalog = {t.name.lower(): t for t in tables}
        found = list(self.rules)
        for rule in self.rules:
            for parent in _ancestors(catalog, rule.table.lower()):
                found.append(Rule(rule.source, catalog[parent].name, rule.column))
        for table, column in self.blocked_columns:
            if table in catalog and column not in {c.lower() for c in catalog[table].columns}:
                found.append(Rule(self.rules[0].source, catalog[table].name))
        return Rules(tuple(dict.fromkeys(found)))

    def notices(self, tables: Iterable[Table]) -> tuple[str, ...]:
        """What the user should know about how these rules hold on this catalog: parents withheld for a blocked
        child, and rules naming a column the table no longer has."""
        catalog = {t.name.lower(): t for t in tables}
        said = []
        for rule in self.rules:
            for parent in _ancestors(catalog, rule.table.lower()):
                what = f"{rule.column} is" if rule.column else "its rows are"
                said.append(f"{catalog[parent].name} holds {rule.table}'s rows, so {what} blocked in "
                            f"{catalog[parent].name} too")
        for table, column in sorted(self.blocked_columns):
            if table in catalog and column not in {c.lower() for c in catalog[table].columns}:
                said.append(f"{table}.{column} is blocked but {table} has no such column any more (renamed?): "
                            f"{table} is kept from the AI until you block the new name or remove the rule")
        return tuple(said)

    def level(self, table: Table, column: str) -> str | None:
        """The level a column of `table` is shown at: in full, unless a rule blocks it (None). Only asked of
        columns `visible` lets through (the AI log records each one's level, grants.Grants.level)."""
        name = table.name.lower()
        if name in self.blocked_tables or (name, column.lower()) in self.blocked_columns:
            return None
        return SHOWN_IN_FULL

    def shape(self, table: Table, column: str) -> str | None:
        """How the AI may use a column (grants.Grants.shape): under block rules, its level."""
        return self.level(table, column)

    def visible(self, tables: Iterable[Table]) -> tuple[Table, ...]:
        """The catalog as the AI may see it: blocked tables and columns left out (as they hold on this catalog, see
        `covering`), tables with no column left dropped, and, once there is any rule, derived tables (views and the
        like, see Table.derived) dropped too."""
        tables = tuple(tables)
        if not self.rules:
            return tuple(Table(t.name, t.columns, schema=t.schema) for t in tables)
        holding = self.covering(tables)
        if holding != self:
            return holding.visible(tables)
        shown = []
        for table in tables:
            if table.name.lower() in self.blocked_tables or table.derived:
                continue
            columns = tuple(c for c in table.columns if (table.name.lower(), c.lower()) not in self.blocked_columns)
            if columns:
                shown.append(Table(table.name, columns, schema=table.schema))
        return tuple(shown)

    def withheld_derived(self, tables: Iterable[Table]) -> tuple[str, ...]:
        """The derived tables kept from the AI only because the source has rules."""
        return tuple(t.name for t in tables if self.rules and t.derived and t.name.lower() not in self.blocked_tables)

    def check_result(self, tables: Sequence[str], columns: Sequence[str]) -> None:
        """The second check, on what came back: refuse a result from a blocked table or with a column named like a
        column blocked in one of the tables the query read."""
        read = {t.lower() for t in tables}
        if read & self.blocked_tables:
            raise QueryRejected("the result came from a table blocked by the user's rules")
        blocked = {c for t, c in self.blocked_columns if t in read}
        for column in columns:
            if column.lower() in blocked:
                raise QueryRejected(
                    f"the result has a column named {column!r}, which the user's rules block; leave it out, or give "
                    "an allowed column another name with AS")

    def explain_refusal(self, tables: Sequence[str], referenced: Sequence[str]) -> None:
        """Raise QueryRejected naming a blocked column the query referenced in one of its tables, if there is one.
        Only explains a failure; it is not a check (an unqualified name may belong to another table)."""
        read = {t.lower() for t in tables}
        named = {c.lower() for c in referenced}
        for table, column in sorted(self.blocked_columns):
            if table in read and column in named:
                raise QueryRejected(f"{table}.{column} is blocked by the user's rules; leave it out of the query")

    def browse(self, tables: Iterable[Table]) -> tuple[tuple[Table, ...], frozenset[tuple[str, str | None]]]:
        """What describe_source shows: every table, with the blocked ones marked."""
        return tuple(tables), self.blocked

    def payload(self) -> list[list[str | None]]:
        """A JSON form, for held pulls and selections (`policy_from` reads it back)."""
        return [[r.source, r.table, r.column] for r in self.rules]


def policy_from(data: Any) -> Policy:
    """A policy from its `payload()`: a list is block rules, an object the applied profiles' grants."""
    if isinstance(data, dict):
        from portward_free.grants import Grants  # grants builds on this module
        return Grants.from_payload(data)
    return Rules(tuple(Rule(s, t, c) for s, t, c in data))


def _ancestors(catalog: dict[str, Table], table: str) -> list[str]:
    """The lower-case names of every table whose rows include `table`'s, nearest first."""
    found: list[str] = []
    waiting = list(catalog[table].parents) if table in catalog else []
    while waiting:
        parent = waiting.pop(0).lower()
        if parent in catalog and parent not in found and parent != table:
            found.append(parent)
            waiting.extend(catalog[parent].parents)
    return found
