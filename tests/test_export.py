"""Any pull can be exported to a CSV or Excel file on the user's machine. The rows go to the file, not to the AI."""

import csv
import sqlite3
from pathlib import Path

from conftest import ORDER_COUNT
from openpyxl import load_workbook
from harness import connected

ORDERS = "SELECT id, customer_id, total FROM orders ORDER BY id"


async def test_a_pull_exports_to_csv_with_every_row_and_the_ai_gets_only_where_it_went(
        home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        pulled = await h.call("query", source="shop", question="All orders", sql=ORDERS)
        exported = await h.call("export", pull_id=pulled.ai.structured["pull_id"], format="csv")
    assert not exported.is_error, exported.ai.text
    file = Path(exported.ai.structured["path"])
    assert file.suffix == ".csv" and file.is_file()
    with file.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["id", "customer_id", "total"]
    assert len(rows) == ORDER_COUNT + 1
    assert rows[1] == ["1", "2", "1.0"] and rows[-1] == [str(ORDER_COUNT), "2", f"{ORDER_COUNT}.0"]
    assert exported.ai.structured["row_count"] == ORDER_COUNT
    assert "rows" not in exported.ai.structured
    assert "| 1 |" not in exported.ai.text
    assert str(file) in exported.ai.text


async def test_a_pull_exports_to_excel_with_typed_cells(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        pulled = await h.call("query", source="shop", question="All orders", sql=ORDERS)
        exported = await h.call("export", pull_id=pulled.ai.structured["pull_id"], format="xlsx")
    assert not exported.is_error, exported.ai.text
    file = Path(exported.ai.structured["path"])
    assert file.suffix == ".xlsx"
    rows = list(load_workbook(file, read_only=True).active.iter_rows(values_only=True))
    assert rows[0] == ("id", "customer_id", "total")
    assert len(rows) == ORDER_COUNT + 1
    assert rows[1] == (1, 2, 1.0)


async def test_text_that_looks_like_a_formula_stays_text_in_excel(home: Path, shop_db: Path) -> None:
    with sqlite3.connect(shop_db) as db:
        db.execute("UPDATE customers SET name = '=1+1' WHERE id = 1")
    db.close()
    async with connected(home, shop_db) as h:
        pulled = await h.call("query", source="shop", question="Names", sql="SELECT name FROM customers ORDER BY id")
        exported = await h.call("export", pull_id=pulled.ai.structured["pull_id"], format="xlsx")
    cell = load_workbook(exported.ai.structured["path"]).active["A2"]
    assert cell.value == "=1+1" and cell.data_type == "s"


async def test_exporting_writes_no_history_entry_and_says_when_the_data_changed(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        pulled = await h.call("query", source="shop", question="Names", sql="SELECT name FROM customers")
        same = await h.call("export", pull_id=pulled.ai.structured["pull_id"])
        with sqlite3.connect(shop_db) as db:
            db.execute("INSERT INTO customers VALUES (4, 'Barbara', 'south')")
        db.close()
        changed = await h.call("export", pull_id=pulled.ai.structured["pull_id"])
        assert len(h.stored("pulls")) == 1
    assert same.ai.structured["same_result"] is True and same.ai.structured["format"] == "csv"
    assert changed.ai.structured["same_result"] is False and changed.ai.structured["row_count"] == 4
    assert "changed" in changed.ai.text
    assert same.ai.structured["path"] != changed.ai.structured["path"]


async def test_an_unknown_pull_or_format_is_an_error_and_writes_no_file(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        pulled = await h.call("query", source="shop", question="Names", sql="SELECT name FROM customers")
        unknown = await h.call("export", pull_id="nope")
        bad = await h.call("export", pull_id=pulled.ai.structured["pull_id"], format="pdf")
    assert unknown.is_error and "history" in unknown.ai.text
    assert bad.is_error and "csv" in bad.ai.text and "xlsx" in bad.ai.text
    assert not (home / "exports").exists()
