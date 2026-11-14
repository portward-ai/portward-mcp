"""Totals-only grants and fall to hidden (#94): a column granted as totals is read only through SUM, AVG and COUNT,
groups under the threshold are dropped, and a grant a connection can't apply as written is hidden and reported."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from test_profiles import User, apply, columns_of, make, seen, tool

from portward_free.grants import Grants
from portward_free.guard import QueryRejected
from portward_free.profiles import Grant, preview
from portward_free.pull import run_pull
from portward_free.sources import SourceError, Table
from portward_free.sources.base import is_numeric_type
from portward_free.sources.files import open_csv
from portward_free.sources.sqlite import SqliteSource

# north: 6 sales with different amounts; south: 2 sales (under the threshold of 5)
SALES = [(i, "north", f"rep{i}", 100.0 + i, f"note {i}") for i in range(1, 7)] + \
        [(7, "south", "rep7", 7777.0, "note 7"), (8, "south", "rep8", 8888.0, "note 8")]


@pytest.fixture()
def sales_db(tmp_path: Path) -> Path:
    path = tmp_path / "sales.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT, rep TEXT, amount REAL, memo TEXT)")
        db.executemany("INSERT INTO sales VALUES (?, ?, ?, ?, ?)", SALES)
    db.close()
    return path


def totals_on_amount() -> Grants:
    return Grants.of([Grant.parse("sales.id"), Grant.parse("sales.region"), Grant.parse("sales.rep"),
                      Grant.parse("sales.amount", level="totals")])


def pull(db: Path, sql: str, grants: Grants | None = None):
    source = SqliteSource(db)
    try:
        return run_pull("sales", source, "?", sql, grants or totals_on_amount())
    finally:
        source.close()


def test_a_totals_column_can_be_summed_and_groups_under_the_threshold_are_dropped(sales_db: Path) -> None:
    found = pull(sales_db, "SELECT region, SUM(amount) AS total, COUNT(*) AS n FROM sales GROUP BY region")
    assert found.rows == (("north", sum(100.0 + i for i in range(1, 7)), 6),)
    assert "fewer than 5" in found.note


@pytest.mark.parametrize("sql", [
    "SELECT amount FROM sales",
    "SELECT * FROM sales",
    "SELECT region FROM sales WHERE amount > 7000",
    "SELECT region, MAX(amount) FROM sales GROUP BY region",
    "SELECT region, SUM(amount) FROM sales GROUP BY region HAVING COUNT(*) > 0",
    "SELECT SUM(amount) FROM (SELECT amount FROM sales WHERE id = 7) AS s",
    "SELECT rep, SUM(amount) OVER () FROM sales",
    "SELECT COUNT(DISTINCT amount) FROM sales",
    "SELECT region FROM sales ORDER BY amount",
])
def test_anything_but_sum_avg_or_count_of_a_totals_column_is_refused_with_a_plain_reason(
        sales_db: Path, sql: str) -> None:
    with pytest.raises(QueryRejected) as refused:
        pull(sales_db, sql)
    message = str(refused.value)
    assert "7777" not in message and "8888" not in message
    assert any(word in message for word in ("SUM", "total", "TOTAL", "SELECT *", "HAVING", "WITH")), message


def test_one_lone_row_cant_be_read_back_through_a_total(sales_db: Path) -> None:
    found = pull(sales_db, "SELECT rep, SUM(amount) FROM sales GROUP BY rep")
    assert found.rows == (("other (small groups)", sum(s[3] for s in SALES)),)  # every rep merged (#122)
    whole = pull(sales_db, "SELECT SUM(amount) FROM sales WHERE id = 7")
    assert whole.rows == ()


def test_full_columns_of_the_same_table_still_read_as_rows(sales_db: Path) -> None:
    found = pull(sales_db, "SELECT rep FROM sales WHERE region = 'south' ORDER BY rep")
    assert found.rows == (("rep7",), ("rep8",))
    assert found.note == ""


# --- fall to hidden -------------------------------------------------------------------------------------------------

def test_totals_on_free_text_falls_to_hidden_and_the_preview_says_why(sales_db: Path) -> None:
    source = SqliteSource(sales_db)
    catalog = source.tables()
    source.close()
    grants = [Grant.parse("sales.region"), Grant.parse("sales.memo", level="totals"),
              Grant.parse("sales.amount", level="totals")]
    found = preview(grants, catalog)
    assert [g.name for g in found.matched] == ["sales.region", "sales.amount"]
    assert [(g.name, "number" in why) for g, why in found.hidden_here] == [("sales.memo", True)]
    assert "hidden here instead: sales.memo" in found.text()
    policy = Grants.of(grants)
    assert policy.visible(catalog)[0].columns == ("region", "amount")
    assert not any("memo" in n for n in policy.notices(catalog))  # the AI is never told what's hidden, or why


@pytest.mark.parametrize("sql", ["SELECT memo FROM sales", "SELECT COUNT(memo) FROM sales", "SELECT * FROM sales"])
def test_a_fallen_grant_is_never_shown_in_full(sales_db: Path, sql: str) -> None:
    grants = Grants.of([Grant.parse("sales.region"), Grant.parse("sales.memo", level="totals")])
    try:
        found = pull(sales_db, sql, grants)
    except (QueryRejected, SourceError):
        return
    assert "memo" not in found.columns and not any("note" in str(v) for r in found.rows for v in r)


def test_totals_falls_to_hidden_when_the_source_doesnt_say_a_column_is_a_number() -> None:
    catalog = (Table("sales", ("amount",), schema="main"),)  # no types reported
    found = preview([Grant.parse("sales.amount", level="totals")], catalog)
    assert found.nothing_matches and len(found.hidden_here) == 1
    assert Grants.of([Grant.parse("sales.amount", level="totals")]).visible(catalog) == ()


@pytest.mark.parametrize("declared, numeric", [
    ("INTEGER", True), ("REAL", True), ("DECIMAL(10,2)", True), ("int unsigned", True), ("NUMERIC", True),
    ("TEXT", False), ("VARCHAR(20)", False), ("", False), ("BOOLEAN", False), ("DATE", False), ("POINT", False),
    ("INTERVAL", False),
])
def test_only_number_types_count_as_numbers(declared: str, numeric: bool) -> None:
    assert is_numeric_type(declared) is numeric


def test_a_csv_file_reports_its_number_columns(tmp_path: Path) -> None:
    path = tmp_path / "sales.csv"
    path.write_text("region,amount,memo\nnorth,10.5,hi\nsouth,3,there\n")
    source = open_csv({"path": str(path)})
    try:
        (table,) = source.tables()
    finally:
        source.close()
    assert table.numeric == frozenset({"amount"})


def test_a_csv_totals_column_drops_small_groups(tmp_path: Path) -> None:
    path = tmp_path / "sales.csv"
    lines = [f"{region},{rep},{amount}\n" for _, region, rep, amount, _ in SALES]
    path.write_text("region,rep,amount\n" + "".join(lines))
    source = open_csv({"path": str(path)})
    grants = Grants.of([Grant.parse("sales.region"), Grant.parse("sales.amount", level="totals")])
    try:
        found = run_pull("csv", source, "?", "SELECT region, AVG(amount) AS a FROM sales GROUP BY region", grants)
    finally:
        source.close()
    assert found.rows == (("north", 103.5),)


# --- the profile tools ----------------------------------------------------------------------------------------------

async def test_a_profile_grants_columns_as_totals_and_applying_reports_what_falls_to_hidden(
        home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        made = await make(h, "Finance", ["orders.id"], totals=["orders.total", "customers.name"])
        applied = await apply(h, "Finance")
        summed = await h.call("query", source="people", question="?", sql="SELECT SUM(total) AS s FROM orders")
        rows = await h.call("query", source="people", question="?", sql="SELECT total FROM orders")
        names = await h.call("query", source="people", question="?", sql="SELECT COUNT(name) FROM customers")
        listed = await h.call("list_sources")
    assert {g["name"]: g["level"] for g in made.ai.structured["profile"]["grants"]} == {
        "orders.id": "full", "orders.total": "totals", "customers.name": "totals"}
    preview = applied.ai.structured["preview"]
    assert preview["counts"] == {"matched": 2, "hidden_here": 1, "not_in_your_data": 0}
    assert preview["hidden_here"][0]["grant"] == "customers.name" and "number" in preview["hidden_here"][0]["why"]
    assert not summed.is_error, summed.ai.text
    assert summed.ai.structured["rows"] == [] and "fewer than 5" in summed.ai.text  # 3 orders: under the threshold
    assert rows.is_error and "SUM(total)" in rows.ai.text and "10.0" not in rows.ai.text
    assert names.is_error and "Ada" not in seen(names)
    assert "customers" not in columns_of(listed)
    assert any("orders.total is totals only" in w for w in listed.ai.structured["sources"][0]["warnings"])


async def test_adding_a_totals_grant_to_an_applied_profile_says_it_is_totals_only(
        home: Path, people_db: Path) -> None:
    user = User()
    async with tool(home, people_db, user=user) as h:
        await make(h, "Finance", ["orders.id"])
        await apply(h, "Finance")
        saved = await h.call("update_profile", profile="Finance", totals=["orders.total"])
    assert not saved.is_error, saved.ai.text
    assert any("orders.total" in q and "totals" in q for q in user.asked)
