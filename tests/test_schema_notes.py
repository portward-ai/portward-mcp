"""Users browse a source's tables and columns and write notes on what they mean; the AI gets those notes."""

from pathlib import Path

from harness import Harness, connected


async def test_a_sources_tables_and_columns_can_be_browsed(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        reply = await h.call("describe_source", source="shop")
    assert not reply.is_error, reply.ai.text
    assert reply.ai.structured == {"source": "shop", "tables": [
        {"name": "customers", "note": None, "blocked": False, "columns": [
            {"name": "id", "note": None, "blocked": False},
            {"name": "name", "note": None, "blocked": False},
            {"name": "region", "note": None, "blocked": False}]},
        {"name": "orders", "note": None, "blocked": False, "columns": [
            {"name": "id", "note": None, "blocked": False},
            {"name": "customer_id", "note": None, "blocked": False},
            {"name": "total", "note": None, "blocked": False}]},
    ]}


def notes_of(described: dict) -> dict[str, str]:
    """`table` or `table.column` -> note, for the items that have one."""
    found = {}
    for table in described["tables"]:
        if table["note"] is not None:
            found[table["name"]] = table["note"]
        for column in table["columns"]:
            if column["note"] is not None:
                found[f"{table['name']}.{column['name']}"] = column["note"]
    return found


async def test_notes_written_on_a_table_and_a_column_are_kept_after_a_restart(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        on_table = await h.call("set_note", source="shop", table="orders", note="One row per checkout")
        on_column = await h.call("set_note", source="shop", table="orders", column="total",
                                 note="Amount in euros, tax included")
    assert not on_table.is_error, on_table.ai.text
    assert not on_column.is_error, on_column.ai.text
    async with Harness.start(home) as h:
        reply = await h.call("describe_source", source="shop")
    assert notes_of(reply.ai.structured) == {"orders": "One row per checkout",
                                             "orders.total": "Amount in euros, tax included"}


async def test_editing_a_note_replaces_it_and_an_empty_note_removes_it(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        await h.call("set_note", source="shop", table="customers", column="region", note="Sales region")
        await h.call("set_note", source="shop", table="customers", column="region", note="north or south")
        await h.call("set_note", source="shop", table="customers", note="People who bought")
        await h.call("set_note", source="shop", table="customers", note="  ")
        reply = await h.call("describe_source", source="shop")
    assert notes_of(reply.ai.structured) == {"customers.region": "north or south"}


async def test_a_note_on_a_table_or_column_the_source_does_not_have_is_refused(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        no_table = await h.call("set_note", source="shop", table="invoices", note="x")
        no_column = await h.call("set_note", source="shop", table="orders", column="tax", note="x")
        reply = await h.call("describe_source", source="shop")
    assert no_table.is_error and "invoices" in no_table.ai.text
    assert no_column.is_error and "tax" in no_column.ai.text
    assert notes_of(reply.ai.structured) == {}


async def test_the_ai_gets_the_notes_with_the_tables_it_can_query(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        await h.call("set_note", source="shop", table="orders", note="One row per checkout")
        await h.call("set_note", source="shop", table="orders", column="total", note="Euros, tax included")
        listed = await h.call("list_sources")
    tables = {t["name"]: t for t in listed.ai.structured["sources"][0]["tables"]}
    assert tables["orders"] == {"name": "orders", "columns": ["id", "customer_id", "total"],
                                "note": "One row per checkout", "column_notes": {"total": "Euros, tax included"}}
    assert tables["customers"] == {"name": "customers", "columns": ["id", "name", "region"]}
    assert "Euros, tax included" in listed.ai.text
