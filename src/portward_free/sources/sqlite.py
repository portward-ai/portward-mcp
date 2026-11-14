"""A SQLite file, opened read-only.

Three locks besides the guard: the file is opened `mode=ro`, `query_only` refuses writes, and an authorizer allows
only reading (SELECT, READ, FUNCTION, RECURSIVE) while a query runs. A progress handler stops long queries.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from portward_free.sources.base import QUERY_TIMEOUT_SECONDS, Fetched, SourceError, Table, is_numeric_type
from portward_free.stand_ins import checked_group, stand_in_for

PROGRESS_EVERY = 10_000  # SQLite VM steps between timeout checks
STAND_IN_FUNCTION = "pw_stand_in"  # the guard refuses it in the AI's own SQL (not on its function allow-list)
READ_ACTIONS = frozenset({sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                          getattr(sqlite3, "SQLITE_RECURSIVE", 33)})


class SqliteSource:
    dialect = "sqlite"
    warnings: tuple[str, ...] = ()

    def __init__(self, path: Path) -> None:
        self._blocked_tables: frozenset[str] = frozenset()
        self._blocked_columns: frozenset[tuple[str, str]] = frozenset()
        if not path.is_file():
            raise SourceError(f"no SQLite file at {path}")
        try:
            self._db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
            self._db.execute("PRAGMA query_only = ON")
            self._db.execute("SELECT count(*) FROM sqlite_master").fetchone()  # not a database: fails here
        except sqlite3.DatabaseError as err:
            raise SourceError(f"can't open {path.name} as a SQLite database: {err}") from None

    def tables(self) -> tuple[Table, ...]:
        names = [r[0] for r in self._db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        tables = []
        for name in names:
            info = self._db.execute("SELECT name, type FROM pragma_table_info(?)", (name,)).fetchall()
            tables.append(Table(name, tuple(c for c, _ in info), schema="main",
                                numeric=frozenset(c for c, kind in info if is_numeric_type(kind))))
        return tuple(tables)

    def use_stand_in_key(self, key: bytes) -> None:
        self._db.create_function(STAND_IN_FUNCTION, 2, lambda value, group: stand_in_for(key, value, group),
                                 deterministic=True)

    def stand_in_sql(self, column_sql: str, group: str) -> str:
        return f"{STAND_IN_FUNCTION}(CAST({column_sql} AS TEXT), '{checked_group(group)}')"

    def block_reads(self, tables: frozenset[str], columns: frozenset[tuple[str, str]]) -> None:
        self._blocked_tables = frozenset(t.lower() for t in tables)
        self._blocked_columns = frozenset((t.lower(), c.lower()) for t, c in columns)

    def _authorizer(self, denied: list[str]) -> Callable[..., int]:
        """Only reads, and no read of a blocked table or column. SQLite reports each column read on the real
        table, also inside views, and a rowid read under the INTEGER PRIMARY KEY column's name. Blocked reads are
        noted in `denied`."""
        def authorize(action: int, arg1: str | None, arg2: str | None, *_: Any) -> int:
            if action not in READ_ACTIONS:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ:
                table, column = (arg1 or "").lower(), (arg2 or "").lower()
                if table in self._blocked_tables:
                    denied.append(f"table {table}")
                    return sqlite3.SQLITE_DENY
                if (table, column) in self._blocked_columns:
                    denied.append(f"{table}.{column}")
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        return authorize

    def fetch(self, sql: str) -> Fetched:
        timeout = QUERY_TIMEOUT_SECONDS
        deadline = time.monotonic() + timeout
        denied: list[str] = []
        self._db.set_authorizer(self._authorizer(denied))
        self._db.set_progress_handler(lambda: int(time.monotonic() > deadline), PROGRESS_EVERY)
        try:
            cursor = self._db.execute(sql)
            rows = tuple(tuple(r) for r in cursor.fetchall())
        except sqlite3.DatabaseError as err:
            if denied:  # e.g. through a view, or rowid for a blocked primary key
                raise SourceError(f"the query reads {denied[0]}, which is blocked by the user's rules (through a view "
                                  "or rowid); leave it out of the query") from None
            if isinstance(err, sqlite3.OperationalError) and time.monotonic() > deadline:
                raise SourceError(f"the query took longer than {timeout:g} seconds and was stopped") from None
            raise SourceError(f"the query failed: {err}") from None
        except sqlite3.Error as err:
            raise SourceError(f"the query failed: {err}") from None
        finally:
            self._db.set_authorizer(None)
            self._db.set_progress_handler(None, 0)
        return Fetched(columns=tuple(d[0] for d in cursor.description or ()), rows=rows)

    def close(self) -> None:
        self._db.close()


def open_sqlite(settings: Mapping[str, Any], password: str | None = None) -> SqliteSource:
    path = settings.get("path")
    if not isinstance(path, str) or not path.strip():
        raise SourceError("a SQLite source needs `path`, the file's location")
    return SqliteSource(Path(path).expanduser())
