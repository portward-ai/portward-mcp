"""CSV and Excel files, queried with an in-memory DuckDB.

Lifted from the gateway's DuckDB file source and trimmed (no globs, no Parquet; stand-ins are made in SQL, not a
Python function). A CSV file is one table named after the file; an Excel workbook is one table per sheet, read with
openpyxl (so no DuckDB extension has to ship). Besides the guard, DuckDB itself is locked down once the tables exist: it may read only the file the user
added, no setting can be changed afterwards, and a watchdog stops long queries.
"""

from __future__ import annotations

import re
import threading
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import duckdb
from openpyxl import load_workbook

from portward_free.sources.base import Fetched, SourceError, Table, is_numeric_type
from portward_free.stand_ins import HEX_CHARS, PREFIX, checked_group, hmac_pads

QUERY_TIMEOUT_SECONDS = 30.0
MEMORY_LIMIT = "1GB"
THREADS = 2
PAD_VARIABLES = ("pw_stand_in_ipad", "pw_stand_in_opad")  # the anonymising key's HMAC pads, per cursor

Loader = Callable[[duckdb.DuckDBPyConnection, Path], list[Table]]


def table_name(label: str, taken: Iterable[str] = ()) -> str:
    """A bare SQL name for a file or sheet: lower case, runs of anything but letters and digits become `_`, and a
    number is added if another table already has the name."""
    name = re.sub(r"[^0-9a-z]+", "_", label.lower()).strip("_") or "table"
    name = f"t_{name}" if name[0].isdigit() else name
    return _unique(name, {t.lower() for t in taken})


def _unique(name: str, taken: set[str]) -> str:
    candidate, n = name, 1
    while candidate.lower() in taken:
        n += 1
        candidate = f"{name}_{n}"
    return candidate


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


class FileSource:
    """One in-memory DuckDB holding a view (CSV) or table (Excel) per table of the file."""

    dialect = "duckdb"
    warnings: tuple[str, ...] = ()

    def __init__(self, path: Path, load: Loader) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._pads: tuple[str, str] | None = None
        self._db = duckdb.connect(":memory:")
        try:
            self._tables = tuple(load(self._db, path))
            self._lock_down()
        except BaseException:
            self._db.close()
            raise

    def _lock_down(self) -> None:
        """Only the added file stays readable, and nothing can be changed after."""
        self._db.execute("SET allowed_paths = ?", [[str(self._path)]])
        self._db.execute("SET allowed_directories = ?", [[]])
        self._db.execute(f"SET memory_limit = '{MEMORY_LIMIT}'")
        self._db.execute(f"SET threads = {THREADS}")
        self._db.execute("SET autoinstall_known_extensions = false")
        self._db.execute("SET autoload_known_extensions = false")
        self._db.execute("SET enable_external_access = false")
        self._db.execute("SET lock_configuration = true")

    def tables(self) -> tuple[Table, ...]:
        return self._tables

    def use_stand_in_key(self, key: bytes) -> None:
        inner, outer = hmac_pads(key)
        with self._lock:
            self._pads = (inner.hex(), outer.hex())

    def stand_in_sql(self, column_sql: str, group: str) -> str:
        """HMAC-SHA256 from DuckDB's sha256 and the key's pads, which are set as variables on the query's cursor
        (never in the SQL text). The AI can't read them back: the guard refuses getvariable."""
        ipad, opad = (f"getvariable('{name}')" for name in PAD_VARIABLES)
        inner = f"sha256({ipad} || encode('{PREFIX}|{checked_group(group)}|' || CAST({column_sql} AS VARCHAR)))"
        return f"'{PREFIX}_' || substr(sha256({opad} || unhex({inner})), 1, {HEX_CHARS})"

    def fetch(self, sql: str) -> Fetched:
        with self._lock:  # one query at a time; the watchdog bounds how long it holds the lock
            cursor = self._db.cursor()
            watchdog = threading.Timer(QUERY_TIMEOUT_SECONDS, cursor.interrupt)
            watchdog.start()
            try:
                if self._pads is not None:
                    for name, pad in zip(PAD_VARIABLES, self._pads, strict=True):
                        cursor.execute(f"SET VARIABLE {name} = unhex(?)", [pad])
                cursor.execute(sql)
                columns = tuple(d[0] for d in cursor.description or ())
                return Fetched(columns=columns, rows=tuple(tuple(r) for r in cursor.fetchall()))
            except duckdb.InterruptException:
                raise SourceError(
                    f"the query took longer than {QUERY_TIMEOUT_SECONDS:g} seconds and was stopped") from None
            except duckdb.Error as err:
                raise SourceError(f"the query failed: {str(err).splitlines()[0]}") from None
            finally:
                watchdog.cancel()

    def close(self) -> None:
        with self._lock:
            self._db.close()


# CSV


def _load_csv(db: duckdb.DuckDBPyConnection, path: Path) -> list[Table]:
    name = table_name(path.stem)
    try:
        db.execute(f"CREATE VIEW {_quoted(name)} AS SELECT * FROM read_csv_auto({_literal(str(path))})")
        described = db.execute(f"DESCRIBE {_quoted(name)}").fetchall()
    except duckdb.Error as err:
        raise SourceError(f"can't read {path.name} as a CSV file: {str(err).splitlines()[0]}") from None
    return [Table(name, tuple(row[0] for row in described), schema="main",
                  numeric=frozenset(row[0] for row in described if is_numeric_type(row[1])))]


# Excel


def _column_type(values: Sequence[Any]) -> str:
    """The DuckDB type for one sheet column, from the Python types openpyxl read; mixed or other types are text."""
    kinds = {type(v) for v in values if v is not None}
    if not kinds:
        return "VARCHAR"
    if kinds == {bool}:
        return "BOOLEAN"
    if kinds == {int}:
        return "BIGINT"
    if kinds <= {int, float}:
        return "DOUBLE"
    if kinds == {date}:
        return "DATE"
    if kinds <= {date, datetime}:
        return "TIMESTAMP"
    if kinds == {time}:
        return "TIME"
    return "VARCHAR"


def _headers(row: Sequence[Any]) -> list[str]:
    """Column names from a sheet's first row: blanks become column_N, repeats get a number."""
    names: list[str] = []
    for i, cell in enumerate(row, start=1):
        label = str(cell).strip() if cell is not None else ""
        names.append(_unique(label or f"column_{i}", {n.lower() for n in names}))
    return names


def _sheet_rows(sheet: Any) -> tuple[list[str], list[list[Any]]] | None:
    """The header and data rows of one sheet, without blank rows or trailing blank columns; None if it is empty."""
    rows = [list(r) for r in sheet.iter_rows(values_only=True) if any(v is not None for v in r)]
    if not rows:
        return None
    width = max(max(i + 1 for i, v in enumerate(r) if v is not None) for r in rows)
    padded = [(r + [None] * width)[:width] for r in rows]
    return _headers(padded[0]), padded[1:]


def _load_sheet(db: duckdb.DuckDBPyConnection, name: str, headers: list[str], rows: list[list[Any]]) -> Table:
    types = [_column_type([r[i] for r in rows]) for i in range(len(headers))]
    columns = ", ".join(f"{_quoted(h)} {t}" for h, t in zip(headers, types, strict=True))
    db.execute(f"CREATE TABLE {_quoted(name)} ({columns})")
    if rows:
        cells = [[str(v) if t == "VARCHAR" and v is not None else v for v, t in zip(r, types, strict=True)]
                 for r in rows]
        db.executemany(f"INSERT INTO {_quoted(name)} VALUES ({', '.join('?' * len(headers))})", cells)
    return Table(name, tuple(headers), schema="main",
                 numeric=frozenset(h for h, t in zip(headers, types, strict=True) if is_numeric_type(t)))


def _load_excel(db: duckdb.DuckDBPyConnection, path: Path) -> list[Table]:
    try:
        book = load_workbook(path, read_only=True, data_only=True)
    except (zipfile.BadZipFile, OSError, KeyError, ValueError) as err:
        raise SourceError(f"can't open {path.name} as an Excel workbook: {err}") from None
    tables: list[Table] = []
    try:
        for sheet in book.worksheets:
            read = _sheet_rows(sheet)
            if read is not None:
                tables.append(_load_sheet(db, table_name(sheet.title, (t.name for t in tables)), *read))
    finally:
        book.close()
    if not tables:
        raise SourceError(f"{path.name} has no sheets with data")
    return tables


# Openers


def _file(settings: Mapping[str, Any], label: str, suffixes: tuple[str, ...]) -> Path:
    raw = settings.get("path")
    if not isinstance(raw, str) or not raw.strip():
        raise SourceError(f"a {label} source needs `path`, the file's location")
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise SourceError(f"no {label} file at {path}")
    if path.suffix.lower() not in suffixes:
        raise SourceError(f"{path.name} is not a {label} file; expected {' or '.join(suffixes)}")
    return path


def open_csv(settings: Mapping[str, Any], password: str | None = None) -> FileSource:
    return FileSource(_file(settings, "CSV", (".csv", ".tsv", ".txt")), _load_csv)


def open_excel(settings: Mapping[str, Any], password: str | None = None) -> FileSource:
    return FileSource(_file(settings, "Excel", (".xlsx", ".xlsm")), _load_excel)
