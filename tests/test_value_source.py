"""Click a number in the results panel to see where it came from (#44).

The panel asks the app-only `value_source` tool about one value (its row and column) of a pull it was lent; the
answer names the source tables and columns behind it and the query's filters, so the user can say where a number
came from. Like the panel's other tools, its result never reaches the AI.
"""

from pathlib import Path

from harness import Harness, connected


async def _source_of(h: Harness, sql: str, column: str, row: int = 0) -> dict:
    reply = await h.call("query", source="shop", question="Where from?", sql=sql)
    assert not reply.is_error, reply.ai.text
    found = await h.call_from_panel("value_source", pull_id=reply.ai.structured["pull_id"], row=row, column=column)
    assert not found.is_error, found.ai.text
    return found.ai.structured


async def test_a_plain_column_names_its_table_column_and_the_filters(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        source = await _source_of(h, "SELECT id, total FROM orders WHERE total > 10 AND customer_id = 2", "total")
    assert source["column"] == "total"
    assert source["kind"] == "column"
    assert source["from"] == [{"table": "orders", "column": "total"}]
    assert source["filters"] == [{"clause": "where", "sql": "orders.total > 10"},
                                 {"clause": "where", "sql": "orders.customer_id = 2"}]


REVENUE_BY_REGION = ("SELECT c.region, SUM(o.total) AS revenue FROM orders o JOIN customers c ON c.id = o.customer_id "
                     "WHERE o.total > 5 GROUP BY c.region HAVING COUNT(*) > 1 ORDER BY c.region")


async def test_an_aggregated_value_names_its_column_the_filters_and_its_group(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        source = await _source_of(h, REVENUE_BY_REGION, "revenue", row=1)
    assert source["kind"] == "aggregate"
    assert source["expression"] == "SUM(orders.total)"  # the alias o is shown as the table it stands for
    assert source["from"] == [{"table": "orders", "column": "total"}]
    assert source["filters"] == [{"clause": "join", "sql": "customers.id = orders.customer_id"},
                                 {"clause": "where", "sql": "orders.total > 5"},
                                 {"clause": "having", "sql": "COUNT(*) > 1"}]
    assert source["group"] == [{"column": "region", "value": "south"}]  # row 1: the south region's total


async def test_a_count_of_rows_names_the_table_it_counts(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        source = await _source_of(h, "SELECT COUNT(*) AS n FROM orders WHERE customer_id = 1", "n")
    assert source["kind"] == "aggregate"
    assert source["from"] == [{"table": "orders", "column": None}]
    assert source["filters"] == [{"clause": "where", "sql": "orders.customer_id = 1"}]


async def test_select_star_and_unqualified_join_columns_trace_through_the_schema(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        star = await _source_of(h, "SELECT * FROM orders LIMIT 5", "total")
        joined = await _source_of(h, "SELECT name, total FROM orders JOIN customers ON customers.id = customer_id",
                                  "name")
    assert star["from"] == [{"table": "orders", "column": "total"}]
    assert star["filters"] == [{"clause": "limit", "sql": "5"}]
    assert joined["from"] == [{"table": "customers", "column": "name"}]


async def test_a_filter_inside_a_with_clause_is_listed_too(home: Path, shop_db: Path) -> None:
    sql = "WITH big AS (SELECT * FROM orders WHERE total > 100) SELECT SUM(total) AS t FROM big"
    async with connected(home, shop_db, panel=True) as h:
        source = await _source_of(h, sql, "t")
    assert source["from"] == [{"table": "orders", "column": "total"}]
    assert {"clause": "where", "sql": "orders.total > 100"} in source["filters"]


async def test_value_source_is_app_only_and_refuses_what_isnt_in_the_result(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        reply = await h.call("query", source="shop", question="q", sql="SELECT id FROM orders")
        pull_id = reply.ai.structured["pull_id"]
        unknown = await h.call_from_panel("value_source", pull_id=pull_id, row=0, column="secret")
        past_end = await h.call_from_panel("value_source", pull_id=pull_id, row=250, column="id")
        no_pull = await h.call_from_panel("value_source", pull_id="nope", row=0, column="id")
        ai_tools = await h.ai_tools()
        logged = [r["tool"] for r in h.stored("ai_responses")]
    assert unknown.is_error and "no column 'secret'" in unknown.ai.text
    assert past_end.is_error and "rows are numbered 0 to 249" in past_end.ai.text
    assert no_pull.is_error and "ask the question again" in no_pull.ai.text
    assert "value_source" not in ai_tools
    assert "value_source" not in logged


async def test_once_block_rules_change_a_held_pull_is_not_traced(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        reply = await h.call("query", source="shop", question="q", sql="SELECT region, COUNT(*) AS n FROM customers "
                                                                       "GROUP BY region")
        await h.call("block", source="shop", table="customers", column="region")
        found = await h.call_from_panel("value_source", pull_id=reply.ai.structured["pull_id"], row=0, column="n")
    assert found.is_error and "block rules changed" in found.ai.text  # its group value would be a blocked value


async def test_without_a_panel_value_source_refuses(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=False) as h:
        reply = await h.call("query", source="shop", question="q", sql="SELECT id FROM orders")
        found = await h.client.call_tool("value_source", {"pull_id": reply.ai.structured["pull_id"], "row": 0,
                                                          "column": "id"})
    assert found.is_error
