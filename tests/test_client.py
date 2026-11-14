"""The server as an MCP client adds it: over stdio, with or without panel support."""

from pathlib import Path

from harness import connected


async def test_added_over_stdio_it_answers_a_question_and_records_the_pull(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, transport="stdio", mode="results") as h:
        reply = await h.call("query", source="shop", question="Customers in the north",
                             sql="SELECT name FROM customers WHERE region = 'north' ORDER BY id")
    assert not reply.is_error, reply.ai.text
    assert reply.ai.structured["rows"] == [["Ada"], ["Linus"]]
    assert [p["question"] for p in h.stored("pulls")] == ["Customers in the north"]


async def test_list_sources_shows_the_tables_and_columns_the_ai_can_query(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        listed = await h.call("list_sources")
    assert not listed.is_error, listed.ai.text
    assert listed.ai.structured == {"sources": [{
        "name": "shop", "kind": "sqlite",
        "tables": [{"name": "customers", "columns": ["id", "name", "region"]},
                   {"name": "orders", "columns": ["id", "customer_id", "total"]}],
    }]}


async def test_with_panel_support_the_query_renders_in_the_panel(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        reply = await h.call("query", source="shop", question="Orders", sql="SELECT id FROM orders")
        panel_page = await h.client.read_resource(reply.tool_meta["ui"]["resourceUri"])
    assert reply.panel is not None
    assert reply.panel.structured_content["pull_id"] == reply.ai.structured["pull_id"]
    assert "<html" in panel_page.contents[0].text.lower()


async def test_without_panel_support_there_is_no_panel(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=False) as h:
        reply = await h.call("query", source="shop", question="Orders", sql="SELECT id FROM orders")
    assert reply.panel is None
    assert not reply.is_error
