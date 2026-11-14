"""Only one plain SELECT runs. Everything else is refused before it reaches the source."""

import sqlite3
from pathlib import Path

import pytest
from harness import connected

REFUSED = [
    "INSERT INTO customers VALUES (9, 'Mallory', 'east')",
    "UPDATE customers SET name = 'x'",
    "DELETE FROM customers",
    "DROP TABLE customers",
    "CREATE TABLE stolen AS SELECT * FROM customers",
    "ALTER TABLE customers ADD COLUMN x TEXT",
    "SELECT * FROM customers; DELETE FROM customers",
    "SELECT 1; SELECT 2",
    "PRAGMA writable_schema = ON",
    "ATTACH DATABASE '/tmp/x.db' AS x",
    "WITH gone AS (DELETE FROM customers RETURNING *) SELECT * FROM gone",
    "SELECT load_extension('evil')",
    "SELECT * FROM pragma_table_info('customers')",
    "SELECT * FROM sqlite_master",
    "SELECT * FROM nowhere",
    "VACUUM INTO '/tmp/copy.db'",
]


def customers(path: Path) -> list[tuple]:
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT * FROM customers ORDER BY id").fetchall()
    db.close()
    return rows


@pytest.mark.parametrize("sql", REFUSED)
async def test_anything_but_one_select_is_refused_before_it_runs(home: Path, shop_db: Path, sql: str) -> None:
    before = customers(shop_db)
    async with connected(home, shop_db) as h:
        reply = await h.call("query", source="shop", question="?", sql=sql)
    assert reply.is_error
    assert reply.ai.structured is None
    assert "Ada" not in reply.ai.text
    assert customers(shop_db) == before


async def test_a_select_with_joins_ctes_and_functions_runs(home: Path, shop_db: Path) -> None:
    sql = ("WITH big AS (SELECT customer_id, sum(total) AS spent FROM orders GROUP BY customer_id) "
           "SELECT upper(c.name) AS name, b.spent FROM customers c JOIN big b ON b.customer_id = c.id ORDER BY c.id")
    async with connected(home, shop_db, mode="results") as h:
        reply = await h.call("query", source="shop", question="Spend per customer", sql=sql)
    assert not reply.is_error, reply.ai.text
    assert [r[0] for r in reply.ai.structured["rows"]] == ["ADA", "GRACE", "LINUS"]


async def test_a_query_that_runs_too_long_is_stopped_with_a_clear_message(
        home: Path, shop_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("portward_free.sources.sqlite.QUERY_TIMEOUT_SECONDS", 0.2)
    sql = "SELECT count(*) FROM orders a, orders b, orders c, orders d"  # 250^4 rows to count
    async with connected(home, shop_db) as h:
        reply = await h.call("query", source="shop", question="?", sql=sql)
        assert h.stored("pulls") == []
    assert reply.is_error and "took longer than" in reply.ai.text
