"""Every pull is written to the local database file in the app-data folder."""

import json
import re
import sqlite3
from pathlib import Path

from harness import connected, file_bytes

SQL = "SELECT c.name, sum(o.total) AS spent FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.name"


async def test_a_pull_is_written_to_the_local_database_in_the_app_data_folder(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        reply = await h.call("query", source="shop", question="Spend per customer", sql=SQL)
        pulls = h.stored("pulls")
    assert (home / "portward.db").is_file()
    assert len(pulls) == 1
    pull = pulls[0]
    assert reply.ai.structured["pull_id"] == pull["id"]
    assert pull["question"] == "Spend per customer"
    assert pull["sql"] == SQL
    assert pull["source"] == "shop"
    assert json.loads(pull["tables"]) == ["customers", "orders"]
    assert json.loads(pull["columns"]) == ["customer_id", "id", "name", "total"]
    assert pull["row_count"] == 3
    assert re.fullmatch(r"[0-9a-f]{64}", pull["result_hash"])
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", pull["created_at"])
    assert pull["parent_id"] is None and pull["saved_name"] is None
    assert pull["keep_rows"] == 0


async def test_the_result_hash_is_the_same_for_the_same_result_and_differs_otherwise(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        await h.call("query", source="shop", question="a", sql=SQL)
        await h.call("query", source="shop", question="b", sql=SQL)
        await h.call("query", source="shop", question="c", sql="SELECT name FROM customers")
        first, again, other = h.stored("pulls")
    assert first["result_hash"] == again["result_hash"]
    assert first["result_hash"] != other["result_hash"]
    assert first["id"] != again["id"]


async def test_a_refused_or_failed_query_is_not_recorded(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        await h.call("query", source="shop", question="?", sql="DELETE FROM customers")
        await h.call("query", source="shop", question="?", sql="SELECT nope FROM customers")
        assert h.stored("pulls") == []


async def test_result_rows_are_not_stored_by_default(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        await h.call("query", source="shop", question="Customers", sql="SELECT name FROM customers")
        logged = h.stored("ai_responses")
    # every table, the log of what the AI was sent (#47) included: its content is sealed with the keychain key
    assert [r["tool"] for r in logged] == ["add_source", "query"]
    assert b"Grace" not in file_bytes(home)


async def test_show_my_last_pulls_lists_the_most_recent_first_with_their_details(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        ids = []
        for question in ("first", "second", "third"):
            reply = await h.call("query", source="shop", question=question, sql="SELECT name FROM customers")
            ids.append(reply.ai.structured["pull_id"])
        listed = await h.call("history", limit=2)
    assert not listed.is_error
    entries = listed.ai.structured["pulls"]
    assert [e["id"] for e in entries] == [ids[2], ids[1]]
    latest = entries[0]
    assert latest["question"] == "third"
    assert latest["sql"] == "SELECT name FROM customers"
    assert latest["source"] == "shop"
    assert latest["tables"] == ["customers"]
    assert latest["columns"] == ["name"]
    assert latest["row_count"] == 3
    assert re.fullmatch(r"[0-9a-f]{64}", latest["result_hash"])
    assert latest["parent_id"] is None and latest["saved_name"] is None and latest["keep_rows"] is False
    assert "third" in listed.ai.text and "second" in listed.ai.text and "first" not in listed.ai.text


async def test_rerunning_a_pull_runs_its_stored_sql_and_gives_the_same_hash_on_unchanged_data(
        home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        original = await h.call("query", source="shop", question="Spend per customer", sql=SQL)
        rerun = await h.call("rerun", pull_id=original.ai.structured["pull_id"])
        first, second = h.stored("pulls")
    assert not rerun.is_error
    assert rerun.ai.structured["rows"] == original.ai.structured["rows"]
    assert rerun.ai.structured["pull_id"] == second["id"] != first["id"]
    assert rerun.ai.structured["rerun_of"] == first["id"]
    assert rerun.ai.structured["same_result"] is True
    assert second["sql"] == SQL and second["question"] == "Spend per customer" and second["source"] == "shop"
    assert second["result_hash"] == first["result_hash"]


async def test_a_rerun_after_the_data_changed_gives_a_different_hash(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        original = await h.call("query", source="shop", question="Customers", sql="SELECT name FROM customers")
        with sqlite3.connect(shop_db) as db:
            db.execute("INSERT INTO customers VALUES (4, 'Barbara', 'south')")
        db.close()
        rerun = await h.call("rerun", pull_id=original.ai.structured["pull_id"])
        first, second = h.stored("pulls")
    assert rerun.ai.structured["same_result"] is False
    assert second["row_count"] == 4
    assert second["result_hash"] != first["result_hash"]


async def test_rerunning_an_unknown_pull_is_an_error_and_records_nothing(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        reply = await h.call("rerun", pull_id="nope")
        assert reply.is_error
        assert "history" in reply.ai.text
        assert h.stored("pulls") == []


async def test_rerun_is_shown_in_the_results_panel(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        original = await h.call("query", source="shop", question="Customers", sql="SELECT name FROM customers")
        rerun = await h.call("rerun", pull_id=original.ai.structured["pull_id"])
    assert rerun.panel is not None
    assert rerun.tool_meta["ui"]["resourceUri"] == original.tool_meta["ui"]["resourceUri"]


async def test_history_is_empty_before_any_pull(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        listed = await h.call("history")
    assert listed.ai.structured["pulls"] == []
    assert "no pulls yet" in listed.ai.text.lower()
