"""What every source connector provides. Add a kind by writing a `Source` and registering its opener in
`sources/__init__.py`."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


QUERY_TIMEOUT_SECONDS = 30.0  # a query that runs longer is stopped
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})  # databases on this machine; others must use TLS


# Type names (upper case, without size or precision) that hold numbers, across SQLite, MySQL and DuckDB. Booleans,
# dates, intervals and anything not listed are not numbers here: a totals-only grant on them falls to hidden.
NUMERIC_TYPES = frozenset({
    "INT", "INTEGER", "TINYINT", "SMALLINT", "MEDIUMINT", "BIGINT", "HUGEINT", "BIG INT", "INT2", "INT4", "INT8",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT", "REAL", "FLOAT", "FLOAT4", "FLOAT8", "DOUBLE",
    "DOUBLE PRECISION", "DECIMAL", "NUMERIC", "NUMBER",
})


def is_numeric_type(name: str | None) -> bool:
    """Whether a column's declared type holds numbers: `DECIMAL(10,2)`, `int unsigned`, `DOUBLE` yes; `TEXT`,
    `BOOLEAN`, `INTERVAL`, `POINT` or no type at all no."""
    base = re.sub(r"\(.*?\)", " ", (name or "").upper())
    words = [w for w in base.split() if w not in ("UNSIGNED", "SIGNED", "ZEROFILL")]
    return " ".join(words) in NUMERIC_TYPES


class SourceError(Exception):
    """The source could not be opened or queried. The message is safe to show to the AI and the user."""


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[str, ...]
    # Its rows come from other tables in a way the guard can't see (a view, a materialized view, a partition or
    # inheritance child). While a source has any block rule, derived tables are kept from the AI, since a view can
    # read a blocked column under another name. A source whose engine blocks reads itself (BlocksReads, like
    # SQLite's authorizer, which holds inside views) leaves it False.
    derived: bool = False
    # Tables whose rows include this one's (a partitioned table or inheritance parent of it): a rule on this table
    # holds on them too (rules.Rules.covering).
    parents: tuple[str, ...] = ()
    # The schema the connection reads tables from (Postgres's current schema, MySQL's database, "main" for SQLite
    # and files); "" when unknown. A profile's grant that names a schema matches only this one.
    schema: str = ""
    # The columns the source reports as numbers (integer, decimal or floating point). A totals-only grant applies
    # only to these; on any other column, or when the source can't tell, it falls to hidden (profiles.hidden_why).
    numeric: frozenset[str] = frozenset()
    # Whether the source can give the AI stand-ins for this table's values (MakesStandIns). An anonymised grant on a
    # table without them falls to hidden (profiles.hidden_why), never to full: a wrapped MCP server (#100) can't,
    # since the stand-in key would be inside SQL that server sees.
    stand_ins: bool = True


@dataclass(frozen=True)
class Fetched:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]


class Source(Protocol):
    dialect: str  # the sqlglot dialect the guard parses this source's SQL with
    warnings: tuple[str, ...]  # things the user should know about this connection (e.g. the login can write)

    def tables(self) -> tuple[Table, ...]: ...

    def fetch(self, sql: str) -> Fetched:
        """Run SQL that the guard has already checked and capped. Must use a read-only connection."""
        ...

    def close(self) -> None: ...


@runtime_checkable
class BlocksReads(Protocol):
    """A source whose engine can refuse reads of named tables and columns itself (SQLite's authorizer). The pull
    calls it before `fetch` when the source has it, as a second lock behind the guard: it also holds inside views,
    which the guard can't see into. Names are lower case; `columns` holds (table, column) pairs."""

    def block_reads(self, tables: frozenset[str], columns: frozenset[tuple[str, str]]) -> None: ...


@runtime_checkable
class MakesStandIns(Protocol):
    """A source that can show a column as stand-ins (portward_free.stand_ins) computed inside the database, so the
    AI's WHERE, GROUP BY and JOIN work on them. A column granted anonymised on a source without this is hidden.
    `use_stand_in_key` is called before `fetch`; the key must never appear in the SQL text."""

    def use_stand_in_key(self, key: bytes) -> None: ...

    def stand_in_sql(self, column_sql: str, group: str) -> str:
        """SQL, in this source's dialect, for the stand-in of the column `column_sql` names, in link group `group`
        (stand_ins.Scopes; hex, checked with stand_ins.checked_group before it goes into the SQL text)."""
        ...


class SourceOpener(Protocol):
    def __call__(self, settings: Mapping[str, Any], password: str | None = None) -> Source:
        """Open the source; `password` comes from the keychain, never from `settings`."""
        ...
