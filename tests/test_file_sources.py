"""CSV and Excel files as sources, through an MCP client: queried like tables, read-only, nothing else readable."""

from datetime import datetime
from pathlib import Path

import pytest
from harness import Harness
from openpyxl import Workbook

from portward_free.sources import SourceError, open_source

SALES_CSV = """region,month,revenue
north,2026-01,1200.5
south,2026-01,800
north,2026-02,1500
"""


@pytest.fixture()
def sales_csv(tmp_path: Path) -> Path:
    """A user's CSV export: 3 rows of monthly revenue by region."""
    path = tmp_path / "Q1 Sales.csv"
    path.write_text(SALES_CSV)
    return path


@pytest.fixture()
def budget_xlsx(tmp_path: Path) -> Path:
    """A user's workbook: two sheets, with numbers, dates, text and a blank cell."""
    book = Workbook()
    people = book.active
    people.title = "Team Members"
    people.append(["Name", "Started", "Salary"])
    people.append(["Ada", datetime(2024, 3, 1), 9000])
    people.append(["Grace", datetime(2025, 7, 15), None])
    costs = book.create_sheet("Costs")
    costs.append(["item", "amount"])
    costs.append(["laptops", 2400.5])
    costs.append(["travel", 600])
    path = tmp_path / "budget.xlsx"
    book.save(path)
    return path


async def test_a_csv_file_is_one_table_named_after_the_file(home: Path, sales_csv: Path) -> None:
    async with Harness.start(home) as h:
        added = await h.add_file("sales", str(sales_csv), kind="csv")
        h.choose_mode("sales", "results")
        assert not added.is_error, added.ai.text
        listed = await h.call("list_sources")
        reply = await h.call("query", source="sales", question="Revenue by region",
                             sql="SELECT region, SUM(revenue) AS total FROM q1_sales GROUP BY region ORDER BY region")
    assert [t["name"] for t in listed.ai.structured["sources"][0]["tables"]] == ["q1_sales"]
    assert listed.ai.structured["sources"] == [{
        "name": "sales", "kind": "csv", "tables": [{"name": "q1_sales", "columns": ["region", "month", "revenue"]}],
    }]
    assert not reply.is_error, reply.ai.text
    assert reply.ai.structured["rows"] == [["north", 2700.5], ["south", 800.0]]


async def test_each_sheet_of_an_excel_file_is_a_table_with_its_types(home: Path, budget_xlsx: Path) -> None:
    async with Harness.start(home) as h:
        added = await h.add_file("budget", str(budget_xlsx), kind="excel")
        h.choose_mode("budget", "results")
        assert not added.is_error, added.ai.text
        listed = await h.call("list_sources")
        people = await h.call("query", source="budget", question="Who started after 2025?",
                              sql="SELECT Name, Salary FROM team_members WHERE Started > DATE '2025-01-01'")
        costs = await h.call("query", source="budget", question="Total costs",
                             sql="SELECT SUM(amount) AS total FROM costs")
    assert listed.ai.structured["sources"][0]["tables"] == [
        {"name": "team_members", "columns": ["Name", "Started", "Salary"]},
        {"name": "costs", "columns": ["item", "amount"]},
    ]
    assert not people.is_error, people.ai.text
    assert people.ai.structured["rows"] == [["Grace", None]]
    assert not costs.is_error, costs.ai.text
    assert costs.ai.structured["rows"] == [[3000.5]]


SECRET = "TOP-SECRET-MARKER"


@pytest.fixture()
def secret_csv(tmp_path: Path) -> Path:
    """A file next to the added one that the user never added."""
    path = tmp_path / "secret.csv"
    path.write_text(f"token\n{SECRET}\n")
    return path


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_csv_auto('{secret}')",
    "SELECT * FROM read_csv('{secret}')",
    "SELECT * FROM '{secret}'",
    'SELECT * FROM "{secret}"',
    "SELECT read_text('{secret}') FROM q1_sales",
    "SELECT * FROM q1_sales, read_text('{secret}')",
    "COPY q1_sales TO '{out}'",
    "COPY (SELECT * FROM q1_sales) TO '{out}'",
    "ATTACH '{out}' AS other",
    "INSTALL httpfs",
    "LOAD httpfs",
    "SET enable_external_access = true",
    "CREATE TABLE copy AS SELECT * FROM q1_sales",
    "DROP VIEW q1_sales",
])
async def test_the_ai_cannot_read_other_files_or_change_anything(
        home: Path, sales_csv: Path, secret_csv: Path, sql: str) -> None:
    out = sales_csv.parent / "out.csv"
    async with Harness.start(home) as h:
        await h.add_file("sales", str(sales_csv), kind="csv")
        h.choose_mode("sales", "results")
        reply = await h.call("query", source="sales", question="?", sql=sql.format(secret=secret_csv, out=out))
        after = await h.call("query", source="sales", question="Rows", sql="SELECT COUNT(*) AS n FROM q1_sales")
    assert reply.is_error
    assert SECRET not in reply.ai.text
    assert not out.exists()
    assert after.ai.structured["rows"] == [[3]]


@pytest.mark.parametrize("kind,fixture", [("csv", "sales_csv"), ("excel", "budget_xlsx")])
def test_even_sql_that_skips_the_guard_cannot_read_another_file(
        kind: str, fixture: str, secret_csv: Path, request: pytest.FixtureRequest) -> None:
    out = secret_csv.parent / "out.csv"
    source = open_source(kind, {"path": str(request.getfixturevalue(fixture))})
    try:
        for sql in (f"SELECT * FROM read_csv_auto('{secret_csv}')", f"SELECT read_text('{secret_csv}')",
                    "SET enable_external_access = true", f"COPY (SELECT 1) TO '{out}'"):
            with pytest.raises(SourceError) as refused:
                source.fetch(sql)
            assert SECRET not in str(refused.value)
    finally:
        source.close()
    assert not out.exists()


async def test_a_missing_or_unreadable_file_is_refused_with_a_clear_message(home: Path, tmp_path: Path) -> None:
    junk = tmp_path / "junk.xlsx"
    junk.write_text("not a workbook")
    empty = tmp_path / "empty.xlsx"
    Workbook().save(empty)
    async with Harness.start(home) as h:
        missing = await h.add_file("a", str(tmp_path / "missing.csv"), kind="csv")
        wrong_type = await h.add_file("b", str(junk), kind="csv")
        not_excel = await h.add_file("c", str(junk), kind="excel")
        no_data = await h.add_file("d", str(empty), kind="excel")
        listed = await h.call("list_sources")
    assert missing.is_error and "no CSV file" in missing.ai.text
    assert wrong_type.is_error and "junk.xlsx is not a CSV file" in wrong_type.ai.text
    assert not_excel.is_error and "can't open junk.xlsx as an Excel workbook" in not_excel.ai.text
    assert no_data.is_error and "no sheets with data" in no_data.ai.text
    assert listed.ai.structured == {"sources": []}


async def test_changes_to_the_file_show_in_the_next_query(home: Path, sales_csv: Path) -> None:
    async with Harness.start(home) as h:
        await h.add_file("sales", str(sales_csv), kind="csv")
        h.choose_mode("sales", "results")
        sales_csv.write_text(SALES_CSV + "south,2026-02,100\n")
        reply = await h.call("query", source="sales", question="Rows", sql="SELECT COUNT(*) AS n FROM q1_sales")
    assert reply.ai.structured["rows"] == [[4]]


async def test_sheet_names_that_clash_once_made_sql_safe_get_distinct_table_names(home: Path, tmp_path: Path) -> None:
    book = Workbook()
    book.active.title = "Sales 2026"
    book.active.append(["n"])
    book.active.append([1])
    other = book.create_sheet("sales-2026")
    other.append(["n", None, "n"])
    other.append([2, None, 3])
    path = tmp_path / "clash.xlsx"
    book.save(path)
    async with Harness.start(home) as h:
        added = await h.add_file("clash", str(path), kind="excel")
        listed = await h.call("list_sources")
    assert not added.is_error, added.ai.text
    assert [t["name"] for t in listed.ai.structured["sources"][0]["tables"]] == ["sales_2026", "sales_2026_2"]
    assert listed.ai.structured["sources"][0]["tables"][1]["columns"] == ["n", "column_2", "n_2"]
