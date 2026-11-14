"""Importing a data dictionary (#96, Personal): a spreadsheet, document or README the user already has, describing
their tables and columns. Portward reads it on this computer, matches each entry onto a table or column the profile
grants, and shows the user that mapping in their client's own prompt; only once they confirm do the descriptions join
the profile as meanings (a new version). Entries about anything the profile doesn't grant are left out, and their names
never reach the AI. Driven through the MCP client harness."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import openpyxl
import pytest
from mcp.types import ElicitResult
from test_context import table_entry
from test_profiles import User, apply, make, seen, tool

ROWS = [("customers", "name", "The customer's legal name, as invoiced"),
        ("customers", "ssn", "zq-hidden-description: social security number"),
        ("orders", "total", "Order value in euros, tax included"),
        ("orders", "", "One row per order placed in the shop")]


def csv_file(folder: Path) -> Path:
    path = folder / "dictionary.csv"
    lines = ["Table,Column,Description"] + [f'{t},{c},"{d}"' for t, c, d in ROWS]
    path.write_text("\n".join(lines) + "\n")
    return path


def xlsx_file(folder: Path) -> Path:
    path = folder / "dictionary.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["Table name", "Field", "Meaning"])
    for row in ROWS:
        sheet.append(list(row))
    book.save(path)
    return path


def readme_file(folder: Path) -> Path:
    """A README: a pipe table for customers, headings and bullets for orders."""
    path = folder / "README.md"
    path.write_text(
        "# Shop data\n\nSome intro text: e.g. this is our warehouse.\n\n"
        "| Table | Column | Description |\n|---|---|---|\n"
        f"| customers | name | {ROWS[0][2]} |\n| customers | ssn | {ROWS[1][2]} |\n\n"
        "## orders\n\n"
        f"{ROWS[3][2]}\n\n"
        f"- `total`: {ROWS[2][2]}\n")
    return path


def docx_file(folder: Path) -> Path:
    """A Word document holding the dictionary as a table."""
    def cell(text: str) -> str:
        return f"<w:tc><w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p></w:tc>"
    rows = "".join(f"<w:tr>{''.join(cell(v) for v in row)}</w:tr>"
                   for row in [("Table", "Column", "Description"), *ROWS])
    body = (f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            f"<w:p><w:r><w:t>Our data dictionary</w:t></w:r></w:p><w:tbl>{rows}</w:tbl></w:body></w:document>")
    path = folder / "dictionary.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", body)
    return path


class Answer:
    """The user answering the mapping prompt: confirm or not, optionally leaving entries out by number."""

    def __init__(self, confirm: bool = True, leave_out: str = "") -> None:
        self.confirm, self.leave_out = confirm, leave_out
        self.asked: list[str] = []

    async def __call__(self, context: Any, params: Any) -> ElicitResult:
        self.asked.append(params.message)
        return ElicitResult(action="accept", content={"confirm": self.confirm, "leave_out": self.leave_out})


GRANTS = ["customers.name", "orders.id", "orders.total"]


@pytest.mark.parametrize("write", [csv_file, xlsx_file, readme_file, docx_file])
async def test_an_imported_dictionary_maps_onto_tables_and_columns_once_the_user_confirms(
        home: Path, people_db: Path, tmp_path: Path, write: Any) -> None:
    answer = Answer()
    async with tool(home, people_db, user=answer) as h:
        await make(h, "Sales", GRANTS)
        await apply(h, "Sales")
        imported = await h.call("import_dictionary", profile="Sales", path=str(write(tmp_path)))
        listed = await h.call("list_sources")
    assert not imported.is_error, imported.ai.text
    (asked,) = answer.asked
    assert "customers.name" in asked and "orders.total" in asked and ROWS[0][2] in asked
    assert table_entry(listed, "customers")["column_notes"] == {"name": ROWS[0][2]}
    orders = table_entry(listed, "orders")
    assert orders["column_notes"] == {"total": ROWS[2][2]} and orders["note"] == ROWS[3][2]
    assert imported.ai.structured["imported"] == 3
    assert imported.ai.structured["profile"]["version"] == 2


async def test_nothing_is_imported_when_the_user_says_no(home: Path, people_db: Path, tmp_path: Path) -> None:
    async with tool(home, people_db, user=User(yes=False)) as h:
        await make(h, "Sales", GRANTS)
        await apply(h, "Sales")
        imported = await h.call("import_dictionary", profile="Sales", path=str(csv_file(tmp_path)))
        listed = await h.call("list_sources")
        profiles = await h.call("list_profiles")
    assert imported.is_error
    assert ROWS[0][2] not in seen(listed)
    (profile,) = profiles.ai.structured["profiles"]
    assert profile["version"] == 1 and profile["context"]["meanings"] == []


async def test_entries_about_what_the_profile_doesnt_grant_are_left_out_and_never_reach_the_ai(
        home: Path, people_db: Path, tmp_path: Path) -> None:
    answer = Answer()
    async with tool(home, people_db, user=answer) as h:
        await make(h, "Sales", GRANTS)
        await apply(h, "Sales")
        imported = await h.call("import_dictionary", profile="Sales", path=str(csv_file(tmp_path)))
        listed = await h.call("list_sources")
    assert "zq-hidden-description" not in answer.asked[0]
    for reply in (imported, listed):
        assert "zq-hidden-description" not in seen(reply) and "ssn" not in seen(reply)
    assert imported.ai.structured["not_matched"] == 1


async def test_the_user_can_leave_entries_out_of_the_mapping(home: Path, people_db: Path, tmp_path: Path) -> None:
    answer = Answer(leave_out="1")
    async with tool(home, people_db, user=answer) as h:
        await make(h, "Sales", GRANTS)
        await apply(h, "Sales")
        imported = await h.call("import_dictionary", profile="Sales", path=str(csv_file(tmp_path)))
        listed = await h.call("list_sources")
    assert "1. customers.name" in answer.asked[0]
    assert "column_notes" not in table_entry(listed, "customers")
    assert imported.ai.structured["imported"] == 2 and imported.ai.structured["left_out"] == 1


async def test_an_entry_the_names_dont_match_can_be_mapped_by_the_ai(home: Path, people_db: Path,
                                                                   tmp_path: Path) -> None:
    path = tmp_path / "glossary.csv"
    path.write_text("Term,Definition\nCustomer full name,The customer's legal name\nOrder amount,Value in euros\n")
    async with tool(home, people_db, user=Answer()) as h:
        await make(h, "Sales", GRANTS)
        await apply(h, "Sales")
        unmatched = await h.call("import_dictionary", profile="Sales", path=str(path))
        mapped = await h.call("import_dictionary", profile="Sales", path=str(path),
                              mapping={"Customer full name": "customers.name", "Order amount": "orders.total"})
        listed = await h.call("list_sources")
    assert unmatched.is_error and "Customer full name" in unmatched.ai.text
    assert not mapped.is_error, mapped.ai.text
    assert table_entry(listed, "customers")["column_notes"] == {"name": "The customer's legal name"}
    assert table_entry(listed, "orders")["column_notes"] == {"total": "Value in euros"}


async def test_a_file_that_isnt_a_dictionary_is_refused_plainly(home: Path, people_db: Path, tmp_path: Path) -> None:
    path = tmp_path / "photo.png"
    path.write_bytes(b"\x89PNG not a dictionary")
    async with tool(home, people_db, user=Answer()) as h:
        await make(h, "Sales", GRANTS)
        wrong_kind = await h.call("import_dictionary", profile="Sales", path=str(path))
        missing = await h.call("import_dictionary", profile="Sales", path=str(tmp_path / "nope.csv"))
    assert wrong_kind.is_error and ".csv" in wrong_kind.ai.text
    assert missing.is_error and "no file" in missing.ai.text
