"""Totals count people, merge small groups, refuse easy-subtraction shapes (#122): a table's meaning can name its
person column, and then the totals minimum counts different people; groups under the minimum are merged into one
'other (small groups)' row; and query shapes that make subtracting one group from another easy are refused."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from test_context import Reviewer
from test_profiles import apply, make, tool
from test_totals import pull, sales_db  # noqa: F401  (fixture)

from portward_free.context import Meaning
from portward_free.grants import Grants
from portward_free.guard import QueryRejected
from portward_free.profiles import Grant
from portward_free.pull import run_pull
from portward_free.sources.sqlite import SqliteSource

# north: one customer with 40 orders; south: 6 customers with one order each
ORDERS = [(i, "c1", "north", float(i)) for i in range(1, 41)] + \
         [(40 + i, f"c{1 + i}", "south", 100.0 + i) for i in range(1, 7)]


@pytest.mark.parametrize("sql, named", [
    ("SELECT region, SUM(amount) FROM sales GROUP BY ROLLUP(region)", "ROLLUP"),
    ("SELECT region, SUM(amount) FROM sales GROUP BY region WITH ROLLUP", "ROLLUP"),
    ("SELECT region, SUM(amount) FROM sales GROUP BY CUBE(region)", "CUBE"),
    ("SELECT region, SUM(amount) FROM sales GROUP BY GROUPING SETS ((region), ())", "GROUPING SETS"),
    ("SELECT region, SUM(amount), ROW_NUMBER() OVER () AS n FROM sales GROUP BY region", "window"),
    ("SELECT region, SUM(amount), COUNT(*) OVER (PARTITION BY region) FROM sales GROUP BY region", "window"),
    ("SELECT SUM(amount) FROM sales UNION ALL SELECT SUM(amount) FROM sales WHERE id <> 7", "UNION"),
    ("SELECT SUM(amount) FROM sales INTERSECT SELECT SUM(amount) FROM sales WHERE id <> 7", "INTERSECT"),
    ("SELECT SUM(amount) FROM sales EXCEPT SELECT SUM(amount) FROM sales WHERE id <> 7", "EXCEPT"),
])
def test_shapes_that_make_subtracting_groups_easy_are_refused_under_totals(
        sales_db, sql: str, named: str) -> None:  # noqa: F811
    with pytest.raises(QueryRejected) as refused:
        pull(sales_db, sql)
    message = str(refused.value)
    assert named in message and "totals" in message, message


def test_the_same_shapes_still_run_on_a_table_without_totals_columns(sales_db) -> None:  # noqa: F811
    found = pull(sales_db, "SELECT region, ROW_NUMBER() OVER (ORDER BY region) AS n FROM sales WHERE id IN (1, 7)")
    assert found.rows == (("north", 1), ("south", 2))


# --- people, not rows -----------------------------------------------------------------------------------------------

@pytest.fixture()
def orders_db(tmp_path: Path) -> Path:
    path = tmp_path / "orders.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer TEXT, region TEXT, amount REAL)")
        db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", ORDERS)
    db.close()
    return path


def orders_grants(person: str | None = "customer") -> Grants:
    grants = [Grant.parse("orders.id"), Grant.parse("orders.region"),
              Grant.parse("orders.customer"), Grant.parse("orders.amount", level="totals")]
    meanings = [Meaning("orders", None, "One row per order", person=person)] if person else []
    return Grants.of(grants, meanings=meanings)


def pull_orders(db: Path, sql: str, grants: Grants):
    source = SqliteSource(db)
    try:
        return run_pull("orders", source, "?", sql, grants)
    finally:
        source.close()


def test_one_person_with_40_rows_doesnt_pass_a_minimum_of_5(orders_db: Path) -> None:
    found = pull_orders(orders_db, "SELECT SUM(amount) AS s FROM orders WHERE region = 'north'", orders_grants())
    assert found.rows == ()
    assert "people" in found.note


def test_without_a_person_column_totals_count_different_rows_as_today(orders_db: Path) -> None:
    found = pull_orders(orders_db, "SELECT SUM(amount) AS s FROM orders WHERE region = 'north'", orders_grants(None))
    assert found.rows == ((820.0,),)  # 1 + 2 + ... + 40
    assert "rows" in found.note


def test_people_are_counted_however_many_rows_each_has(orders_db: Path) -> None:
    north = pull_orders(orders_db, "SELECT COUNT(amount) AS n FROM orders WHERE region = 'north'", orders_grants())
    south = pull_orders(orders_db, "SELECT COUNT(amount) AS n FROM orders WHERE region = 'south'", orders_grants())
    assert north.rows == ()
    assert south.rows == ((6,),)


def test_a_held_pull_keeps_counting_people_when_it_runs_later(orders_db: Path) -> None:
    kept = Grants.from_payload(orders_grants().payload())
    found = pull_orders(orders_db, "SELECT SUM(amount) AS s FROM orders WHERE region = 'north'", kept)
    assert found.rows == ()


def test_a_person_column_the_connection_hides_refuses_the_total(orders_db: Path) -> None:
    grants = Grants.of([Grant.parse("orders.region"), Grant.parse("orders.amount", level="totals")],
                       meanings=[Meaning("orders", None, "One row per order", person="customer")])
    with pytest.raises(QueryRejected) as refused:
        pull_orders(orders_db, "SELECT SUM(amount) FROM orders", grants)
    assert "customer" in str(refused.value) and "people" in str(refused.value)


# --- naming the person column ---------------------------------------------------------------------------------------

NORTH = "SELECT SUM(amount) AS s FROM orders WHERE region = 'north'"


async def test_a_tables_meaning_names_its_person_column_once_the_user_approves_it(home: Path, orders_db: Path) -> None:
    reviewer = Reviewer()
    async with tool(home, orders_db, user=reviewer) as h:
        await make(h, "Sales", ["orders.id", "orders.region", "orders.customer"], totals=["orders.amount"])
        await apply(h, "Sales")
        drafted = await h.call("draft_meanings", profile="Sales", meanings=[
            {"table": "orders", "meaning": "One row per order", "person": "customer"}])
        before = await h.call("query", source="people", question="?", sql=NORTH)
        (draft_id,) = drafted.ai.structured["drafts"]
        approved = await h.call("approve_context", draft_id=draft_id)
        after = await h.call("query", source="people", question="?", sql=NORTH)
        listed = await h.call("list_sources")
    assert any("people (by customer)" in w for w in listed.ai.structured["sources"][0]["warnings"])
    assert before.ai.structured["rows"] == [[820.0]]
    assert not approved.is_error, approved.ai.text
    assert any("customer" in asked and "people" in asked for asked in reviewer.asked)
    assert after.ai.structured["rows"] == [] and "people" in after.ai.text


@pytest.mark.parametrize("meaning, why", [
    ({"table": "orders", "meaning": "One row per order", "person": "id_card"}, "grant"),
    ({"table": "orders", "column": "region", "meaning": "Sales region", "person": "customer"}, "table"),
])
async def test_a_person_column_must_be_a_granted_column_named_on_the_tables_own_meaning(
        home: Path, orders_db: Path, meaning: dict, why: str) -> None:
    async with tool(home, orders_db) as h:
        await make(h, "Sales", ["orders.id", "orders.region", "orders.customer"], totals=["orders.amount"])
        drafted = await h.call("draft_meanings", profile="Sales", meanings=[meaning])
    assert drafted.is_error and why in drafted.ai.text, drafted.ai.text


# --- small groups merge into one row --------------------------------------------------------------------------------

# north: 6 sales by 6 people; south and east: 3 each by 3 people; r1..r5: one sale each, all by the same person
REGIONAL = [(i, f"n{i}", "north", 100.0 + i) for i in range(1, 7)] + \
           [(10 + i, f"s{i}", "south", 200.0 + i) for i in range(1, 4)] + \
           [(20 + i, f"e{i}", "east", 300.0 + i) for i in range(1, 4)]
LONER = [(30 + i, "solo", f"r{i}", 400.0 + i) for i in range(1, 6)]
OTHER = "other (small groups)"


def regional_db(tmp_path: Path, rows: list[tuple]) -> Path:
    path = tmp_path / "regional.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer TEXT, region TEXT, amount REAL)")
        db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", rows)
    db.close()
    return path


def test_small_groups_appear_as_one_other_row_and_the_totals_still_add_up(tmp_path: Path) -> None:
    db = regional_db(tmp_path, REGIONAL)
    found = pull_orders(db, "SELECT region, SUM(amount) AS total, COUNT(*) AS n FROM orders GROUP BY region "
                            "ORDER BY n DESC, total", orders_grants(None))
    assert found.rows == (("north", 621.0, 6), (OTHER, 1512.0, 6))  # south 606 + east 906
    assert found.columns == ("region", "total", "n")
    everything = pull_orders(db, "SELECT SUM(amount) AS total FROM orders", orders_grants(None))
    assert sum(r[1] for r in found.rows) == everything.rows[0][0]
    assert OTHER in found.note


def test_the_other_row_is_left_out_when_it_is_still_under_the_minimum(tmp_path: Path) -> None:
    db = regional_db(tmp_path, REGIONAL[:9])  # north 6, south 3
    found = pull_orders(db, "SELECT region, AVG(amount) AS a FROM orders GROUP BY region", orders_grants(None))
    assert found.rows == (("north", 103.5),)


def test_the_other_row_counts_people_too(tmp_path: Path) -> None:
    db = regional_db(tmp_path, REGIONAL[:6] + LONER)
    sql = "SELECT region, SUM(amount) AS total FROM orders GROUP BY region ORDER BY total"
    by_rows = pull_orders(db, sql, orders_grants(None))
    by_people = pull_orders(db, sql, orders_grants())
    assert by_rows.rows == (("north", 621.0), (OTHER, 2015.0))  # 5 rows, but all one person's
    assert by_people.rows == (("north", 621.0),)


def test_a_large_group_with_no_value_stays_its_own_row(tmp_path: Path) -> None:
    db = regional_db(tmp_path, REGIONAL + [(40 + i, f"x{i}", None, 500.0 + i) for i in range(1, 6)])
    found = pull_orders(db, "SELECT region, COUNT(amount) AS n FROM orders GROUP BY region ORDER BY n, region",
                        orders_grants(None))
    assert set(found.rows) == {(None, 5), ("north", 6), (OTHER, 6)}


def test_grouping_by_an_expression_merges_the_same_way(tmp_path: Path) -> None:
    db = regional_db(tmp_path, REGIONAL)
    found = pull_orders(db, "SELECT UPPER(region) AS r, SUM(amount) * 2 AS twice FROM orders o "
                            "GROUP BY UPPER(o.region) ORDER BY 2", orders_grants(None))
    assert found.rows == (("NORTH", 1242.0), (OTHER, 3024.0))


def test_a_column_outside_the_groups_and_totals_is_refused(tmp_path: Path) -> None:
    db = regional_db(tmp_path, REGIONAL)
    with pytest.raises(QueryRejected) as refused:
        pull_orders(db, "SELECT region, customer, SUM(amount) FROM orders GROUP BY region", orders_grants(None))
    assert "customer" in str(refused.value) and "GROUP BY" in str(refused.value)
