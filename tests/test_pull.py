"""Asking a question of a SQLite file, through an MCP client."""

import sqlite3
from pathlib import Path

from conftest import ORDER_COUNT
from harness import Harness, connected

from portward_free.modes import AI_ROWS
from portward_free.pull import MAX_FETCH_ROWS


async def test_a_question_about_a_sqlite_file_returns_a_preview_table_to_the_ai(home: Path, shop_db: Path) -> None:
    async with Harness.start(home) as h:
        added = await h.add_file("shop", str(shop_db))
        h.choose_mode("shop", "results")
        assert not added.is_error, added.ai.text
        reply = await h.call("query", source="shop", question="Who are the customers?",
                             sql="SELECT name, region FROM customers ORDER BY id")
    assert not reply.is_error, reply.ai.text
    assert reply.ai.structured["columns"] == ["name", "region"]
    assert reply.ai.structured["rows"] == [["Ada", "north"], ["Grace", "south"], ["Linus", "north"]]
    assert "| Ada | north |" in reply.ai.text


async def test_a_large_result_reaches_the_ai_as_a_capped_preview(home: Path, shop_db: Path) -> None:
    assert AI_ROWS < ORDER_COUNT
    async with connected(home, shop_db, mode="results") as shop:
        reply = await shop.call("query", source="shop", question="All orders", sql="SELECT id, total FROM orders ORDER BY id")
    assert not reply.is_error, reply.ai.text
    assert reply.ai.structured["rows"] == [[i, float(i)] for i in range(1, AI_ROWS + 1)]
    assert reply.ai.structured["row_count"] == ORDER_COUNT
    assert reply.ai.structured["sent_rows"] == AI_ROWS
    assert reply.ai.structured["truncated"] is False
    assert f"| {AI_ROWS + 1} |" not in reply.ai.text  # no row past the preview, in the text either
    assert f"first {AI_ROWS} of {ORDER_COUNT} rows" in reply.ai.text


async def test_a_huge_result_is_cut_at_the_fetch_cap_and_says_so(home: Path, tmp_path: Path) -> None:
    big = tmp_path / "big.sqlite"
    with sqlite3.connect(big) as db:
        db.execute("CREATE TABLE events (id INTEGER)")
        db.executemany("INSERT INTO events VALUES (?)", [(i,) for i in range(MAX_FETCH_ROWS + 5)])
    db.close()
    async with connected(home, big, name="big", mode="results") as h:
        reply = await h.call("query", source="big", question="Every event", sql="SELECT id FROM events")
    assert not reply.is_error, reply.ai.text
    assert reply.ai.structured["row_count"] == MAX_FETCH_ROWS
    assert reply.ai.structured["truncated"] is True
    assert f"first {AI_ROWS} of more than {MAX_FETCH_ROWS} rows" in reply.ai.text


async def test_a_source_that_is_not_a_sqlite_file_is_refused_with_a_clear_message(home: Path, tmp_path: Path) -> None:
    junk = tmp_path / "notes.txt"
    junk.write_text("not a database " * 100)
    async with Harness.start(home) as h:
        missing = await h.add_file("x", str(tmp_path / "missing.sqlite"))
        not_db = await h.add_file("y", str(junk))
        unknown = await h.add_file("z", str(junk), kind="oracle")
        unadded = await h.call("query", source="x", question="?", sql="SELECT 1")
    assert missing.is_error and "no SQLite file" in missing.ai.text
    assert not_db.is_error and "can't open" in not_db.ai.text
    assert unknown.is_error and "can't connect to 'oracle' directly" in unknown.ai.text  # and what it reads instead
    assert unadded.is_error and "no source named 'x'" in unadded.ai.text
    assert h.stored("connections") == []
