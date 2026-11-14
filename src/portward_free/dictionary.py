"""Reading a data dictionary the user already has (#96, Personal) and matching it onto a profile's grants.

A dictionary is a spreadsheet (.csv, .tsv, .xlsx), a document (.docx) or a README / text file (.md, .txt). Portward
reads it on this computer and finds entries: a name (a table, a column, or table.column) and its description.
- Spreadsheets and tables in documents: a header row naming the table, the column (or a name / term) and the
  description, in any of the usual words (`HEADERS`).
- READMEs and documents: pipe tables like the above, `name: description` lines and bullets, and headings naming the
  table the lines below them describe; a plain paragraph right under a table's heading describes that table.

`matched` then maps each entry onto a table or column the profile grants, by name (case, spaces and dashes don't
matter), or as the AI says in `mapping` for names that don't match. An entry about anything the profile doesn't grant
maps to nothing. Nothing here saves anything: dictionary_tools.py asks the user to confirm the mapping first.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from portward_free.context import MAX_TEXT, ContextError, Meaning, new_meaning
from portward_free.profiles import Grant

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ENTRIES = 2000
SPREADSHEETS, DOCUMENTS, TEXTS = (".csv", ".tsv", ".xlsx"), (".docx",), (".md", ".markdown", ".txt")
KINDS = SPREADSHEETS + DOCUMENTS + TEXTS
HEADERS = {
    "table": {"table", "table name", "table_name", "dataset", "entity", "object"},
    "column": {"column", "column name", "column_name", "field", "field name", "field_name", "attribute"},
    "name": {"name", "term", "key", "item", "variable"},
    "text": {"description", "meaning", "definition", "notes", "note", "comment", "comments", "details",
             "explanation", "business meaning", "business definition"},
}
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NAME = r"[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*){0,2}"
LINE = re.compile(rf"^\s*(?:[-*+]\s+)?([`*_]*)({NAME})\1\s*(?::|=|—|–|\s-\s)\s*(.+?)\s*$")
HEADING = re.compile(r"^\s*#{1,6}\s+(.+?)\s*#*\s*$")


class DictionaryError(ValueError):
    """A file that can't be read as a dictionary; the message is safe to show."""


@dataclass(frozen=True)
class Entry:
    """One entry as the dictionary wrote it: `name` as shown, and the table and column it names when it says so."""

    name: str
    text: str
    table: str | None = None
    column: str | None = None


@dataclass(frozen=True)
class Matched:
    mapped: tuple[tuple[Entry, Meaning], ...]
    unmatched: tuple[Entry, ...]


# --- reading --------------------------------------------------------------------------------------------------------

def read_entries(path: Path) -> tuple[Entry, ...]:
    if not path.is_file():
        raise DictionaryError(f"no file at {path}")
    suffix = path.suffix.lower()
    if suffix not in KINDS:
        raise DictionaryError(f"{path.name}: import a dictionary saved as {', '.join(KINDS)}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise DictionaryError(f"{path.name} is over {MAX_FILE_BYTES // (1024 * 1024)} MB; split it up")
    try:
        if suffix == ".xlsx":
            entries = list(_from_xlsx(path))
        elif suffix == ".docx":
            entries = list(_from_docx(path))
        else:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            entries = list(_from_rows(_csv_rows(text, suffix)) if suffix in SPREADSHEETS else _from_text(text))
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError, KeyError, ValueError) as err:
        if isinstance(err, DictionaryError):
            raise
        raise DictionaryError(f"{path.name} couldn't be read as a {suffix} file") from None
    if not entries:
        raise DictionaryError(f"found no entries in {path.name}: give it a header row naming the table, column and "
                              "description, or lines like `table.column: description`")
    return tuple(entries[:MAX_ENTRIES])


def _csv_rows(text: str, suffix: str) -> list[list[str]]:
    delimiter = "\t" if suffix == ".tsv" else ","
    if suffix == ".csv":
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
        except csv.Error:
            pass
    return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


def _roles(header: Sequence[str]) -> dict[str, int] | None:
    """Which cell of a header row holds what; None if it isn't a dictionary's header row."""
    found: dict[str, int] = {}
    for i, cell in enumerate(header):
        word = " ".join(str(cell or "").replace("_", " ").split()).lower().strip(":*`")
        for role, words in HEADERS.items():
            if role not in found and (word in words or word.replace(" ", "_") in words):
                found[role] = i
    if "text" not in found or not ({"column", "name", "table"} & found.keys()):
        return None
    return found


def _from_rows(rows: Iterable[Sequence[object]]) -> Iterator[Entry]:
    """Entries from a table: the first row that reads as a header, then each row under it."""
    roles: dict[str, int] | None = None
    for row in rows:
        cells = [" ".join(str(c).split()) if c is not None else "" for c in row]
        if roles is None:
            roles = _roles(cells)
            continue

        def cell(role: str, cells: list[str] = cells) -> str:
            i = roles.get(role) if roles else None
            return cells[i].strip("`*") if i is not None and i < len(cells) else ""
        text = cell("text")
        table, column, name = cell("table"), cell("column"), cell("name")
        if not text or not (table or column or name):
            continue
        shown = ".".join(p for p in (table, column or name) if p)
        yield Entry(shown, text, table or None, (column or name or None) if table else None)


def _from_xlsx(path: Path) -> Iterator[Entry]:
    import openpyxl

    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet in book.worksheets:
            yield from _from_rows(sheet.iter_rows(values_only=True))
    finally:
        book.close()


def _from_docx(path: Path) -> Iterator[Entry]:
    with zipfile.ZipFile(path) as z:
        body = ElementTree.fromstring(z.read("word/document.xml")).find(f"{W}body")
    if body is None:
        return
    lines: list[str] = []
    for block in body:
        if block.tag == f"{W}tbl":
            rows = [["".join(t.text or "" for t in tc.iter(f"{W}t")) for tc in tr.iter(f"{W}tc")]
                    for tr in block.iter(f"{W}tr")]
            yield from _from_rows(rows)
        elif block.tag == f"{W}p":
            text = "".join(t.text or "" for t in block.iter(f"{W}t"))
            style = block.find(f"{W}pPr/{W}pStyle")
            heading = style is not None and str(style.get(f"{W}val", "")).lower().startswith("heading")
            lines.append(f"## {text}" if heading else text)
    yield from _from_text("\n".join(lines))


def _heading_table(title: str) -> str | None:
    """The table a heading names ("orders", "Table: orders", "`orders` table"), if it names one."""
    words = title.strip().strip("`*")
    words = re.sub(r"^(table|dataset)\s*[:\-]?\s*|\s+(table|dataset)$", "", words, flags=re.IGNORECASE).strip("`* ")
    return words if re.fullmatch(r"[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?", words) else None


def _from_text(text: str) -> Iterator[Entry]:
    table: str | None = None
    under_heading = False
    pipe_rows: list[list[str]] = []
    for raw in [*text.splitlines(), ""]:
        line = raw.strip()
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                pipe_rows.append(cells)
            continue
        if pipe_rows:
            yield from _from_rows(pipe_rows)
            pipe_rows = []
        if heading := HEADING.match(line):
            table, under_heading = _heading_table(heading.group(1)), True
            continue
        if not line:
            continue
        if found := LINE.match(line):
            name, described = found.group(2), found.group(3)
            if "." in name or table is None:
                yield Entry(name, described)
            else:
                yield Entry(f"{table}.{name}", described, table, name)
        elif table and under_heading:
            yield Entry(table, line, table, None)
        under_heading = False


# --- matching -------------------------------------------------------------------------------------------------------

def _norm(name: str) -> str:
    return re.sub(r"[\s\-]+", "_", name.strip().strip("`*")).lower()


def _text(text: str) -> str:
    cleaned = " ".join(text.split())
    return cleaned if len(cleaned) <= MAX_TEXT else cleaned[:MAX_TEXT - 1] + "…"


def _meaning(table: Grant | None, column: str | None, text: str) -> Meaning | None:
    if table is None:
        return None
    try:
        return new_meaning(f"{table.schema}.{table.table}" if table.schema else table.table, column, _text(text))
    except ContextError:
        return None


def _onto(table: str | None, column: str | None, grants: Sequence[Grant]) -> tuple[Grant | None, str | None]:
    """The granted (table, column) that `table`/`column` name: column None for the table itself."""
    if table is not None:
        parts = [_norm(p) for p in table.split(".")]
        tables = [g for g in grants if _norm(g.table) == parts[-1]
                  and (len(parts) == 1 or g.schema is None or _norm(g.schema) == parts[0])]
        if column is None:
            return (tables[0], None) if tables else (None, None)
        found = next((g for g in tables if _norm(g.column) == _norm(column)), None)
        return (found, found.column) if found else (None, None)
    assert column is not None
    if "." in column:
        *rest, last = column.split(".")
        return _onto(".".join(rest), last, grants)
    columns = [g for g in grants if _norm(g.column) == _norm(column)]
    if len({(g.schema, g.table.lower()) for g in columns}) == 1:
        return columns[0], columns[0].column
    if not columns:
        return _onto(column, None, grants)  # maybe a table's own description
    return None, None  # the same column name in several granted tables: the AI maps it


def matched(entries: Iterable[Entry], grants: Sequence[Grant], mapping: Mapping[str, str] | None = None) -> Matched:
    wanted = {_norm(k): v for k, v in (mapping or {}).items()}
    mapped: list[tuple[Entry, Meaning]] = []
    unmatched: list[Entry] = []
    for entry in entries:
        if _norm(entry.name) in wanted:
            target = wanted[_norm(entry.name)].strip()
            grant, column = _onto(target, None, grants) if "." not in target else _onto(None, target, grants)
        elif entry.table is not None:
            grant, column = _onto(entry.table, entry.column, grants)
        else:
            grant, column = _onto(None, entry.name, grants)
        meaning = _meaning(grant, column, entry.text)
        if meaning is None:
            unmatched.append(entry)
        else:
            mapped.append((entry, meaning))
    return Matched(tuple(mapped), tuple(unmatched))
