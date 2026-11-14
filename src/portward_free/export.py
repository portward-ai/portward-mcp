"""Write a pull's rows to a CSV or Excel file on the user's machine.

Files go in the `exports` folder next to the local database, under a fresh name, so an export never overwrites
anything and the AI can't pick where a file is written.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell

EXPORTS_FOLDER = "exports"
MAX_NAME_CHARS = 60


def _write_csv(path: Path, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    with path.open("x", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(["" if v is None else v for v in row] for row in rows)


def _xlsx_cell(sheet: Any, value: Any) -> Any:
    """Numbers and dates stay typed; text is always text, so a value like '=HYPERLINK(...)' never becomes a formula."""
    if value is None or isinstance(value, (bool, int, float, date, datetime, time)):
        return value
    cell = WriteOnlyCell(sheet, value.hex() if isinstance(value, bytes) else str(value))
    cell.data_type = "s"
    return cell


def _write_xlsx(path: Path, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    book = Workbook(write_only=True)
    sheet = book.create_sheet("Pull")
    sheet.append([_xlsx_cell(sheet, c) for c in columns])
    for row in rows:
        sheet.append([_xlsx_cell(sheet, v) for v in row])
    with path.open("xb") as f:
        book.save(f)


WRITERS: dict[str, Callable[[Path, Sequence[str], Sequence[Sequence[Any]]], None]] = {
    "csv": _write_csv,
    "xlsx": _write_xlsx,
}


def _slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()[:MAX_NAME_CHARS].strip("-")
    return slug or "pull"


def export_file(folder: Path, title: str, pull_id: str, fmt: str, columns: Sequence[str],
                rows: Sequence[Sequence[Any]]) -> Path:
    """Write the rows to a new file in `folder` and return its path. `fmt` must be a key of WRITERS."""
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    stem = f"{_slug(title)}-{pull_id[:8]}-{stamp}"
    path = folder / f"{stem}.{fmt}"
    counter = 1
    while path.exists():
        counter += 1
        path = folder / f"{stem}-{counter}.{fmt}"
    WRITERS[fmt](path, columns, rows)
    return path
