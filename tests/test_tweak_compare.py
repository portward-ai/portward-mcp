"""Tweak a past pull ('same, but only the north'), compare two runs, and list an entry's lineage."""

import sqlite3
from pathlib import Path

from conftest import ORDER_COUNT
from harness import MemoryKeychain, connected

from portward_free.modes import AI_ROWS

ALL = "SELECT name, region FROM customers"
NORTH = "SELECT name, region FROM customers WHERE region = 'north'"


async def test_a_tweak_shows_the_sql_difference_and_saves_a_child_of_the_old_pull(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, mode="results") as h:
        original = await h.call("query", source="shop", question="All customers", sql=ALL)
        parent_id = original.ai.structured["pull_id"]
        tweak = await h.call("tweak", pull_id=parent_id, sql=NORTH, question="Same, but only the north")
        parent, child = h.stored("pulls")
    assert not tweak.is_error
    assert tweak.ai.structured["rows"] == [["Ada", "north"], ["Linus", "north"]]
    assert tweak.ai.structured["tweak_of"] == parent_id
    diff = tweak.ai.structured["sql_diff"]
    assert "-SELECT name, region FROM customers\n" in diff
    assert "+SELECT name, region FROM customers WHERE region = 'north'" in diff
    assert diff in tweak.ai.text
    assert child["id"] == tweak.ai.structured["pull_id"]
    assert child["parent_id"] == parent["id"] == parent_id
    assert child["sql"] == NORTH and child["source"] == "shop"
    assert child["question"] == "Same, but only the north"


async def test_tweaking_an_unknown_pull_is_an_error_and_records_nothing(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        reply = await h.call("tweak", pull_id="nope", sql=NORTH)
        assert reply.is_error and "history" in reply.ai.text
        assert h.stored("pulls") == []


async def test_lineage_lists_an_entrys_parent_and_children(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        root = (await h.call("query", source="shop", question="All", sql=ALL)).ai.structured["pull_id"]
        north = (await h.call("tweak", pull_id=root, sql=NORTH)).ai.structured["pull_id"]
        south_sql = "SELECT name, region FROM customers WHERE region = 'south'"
        south = (await h.call("tweak", pull_id=root, sql=south_sql)).ai.structured["pull_id"]
        ada = (await h.call("tweak", pull_id=north, sql=NORTH + " AND name = 'Ada'")).ai.structured["pull_id"]
        of_north = await h.call("lineage", pull_id=north)
        of_root = await h.call("lineage", pull_id=root)
        of_ada = await h.call("lineage", pull_id=ada)
    assert not of_north.is_error
    assert of_north.ai.structured["pull"]["id"] == north
    assert of_north.ai.structured["parent"]["id"] == root
    assert [c["id"] for c in of_north.ai.structured["children"]] == [ada]
    assert of_root.ai.structured["parent"] is None
    assert [c["id"] for c in of_root.ai.structured["children"]] == [north, south]
    assert [a["id"] for a in of_ada.ai.structured["ancestors"]] == [north, root]
    assert of_ada.ai.structured["children"] == []
    assert root in of_north.ai.text and ada in of_north.ai.text


SPEND = ("SELECT c.name, count(*) AS orders, sum(o.total) AS spent FROM customers c "
         "JOIN orders o ON o.customer_id = c.id {where}GROUP BY c.name ORDER BY c.name")


async def test_compare_reports_rows_added_and_removed_between_a_pull_and_its_tweak(
        home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, mode="results") as h:
        before = (await h.call("query", source="shop", question="All", sql=ALL)).ai.structured["pull_id"]
        after = (await h.call("tweak", pull_id=before, sql=NORTH)).ai.structured["pull_id"]
        with sqlite3.connect(shop_db) as db:
            db.execute("INSERT INTO customers VALUES (4, 'Barbara', 'north')")
        db.close()
        reply = await h.call("compare", pull_id=before, other_pull_id=after)
    assert not reply.is_error
    result = reply.ai.structured
    assert result["removed"] == 1 and result["added"] == 0
    assert result["columns"] == ["change", "name", "region"]
    assert result["rows"] == [["removed", "Grace", "south"]]
    assert "Grace" in reply.ai.text


async def test_compare_reports_totals_that_moved(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, mode="results") as h:
        before = (await h.call("query", source="shop", question="Spend",
                               sql=SPEND.format(where="WHERE o.id <= 200 "))).ai.structured["pull_id"]
        after = (await h.call("tweak", pull_id=before, sql=SPEND.format(where=""))).ai.structured["pull_id"]
        reply = await h.call("compare", pull_id=before, other_pull_id=after)
    result = reply.ai.structured
    assert result["added"] == 0 and result["removed"] == 0
    assert result["changed"] == 3 and result["moved_totals"] == ["orders", "spent"]
    totals = {r[0]: r[1:] for r in result["rows"] if r[0].startswith("total")}  # 6 changed rows fit the preview
    assert totals["total before"] == [None, 200, 20100.0]
    assert totals["total after"] == [None, 250, 31375.0]
    assert totals["total change"] == [None, 50, 11275.0]
    assert "spent" in reply.ai.text and "11275" in reply.ai.text


async def test_comparing_two_runs_of_the_same_sql_without_kept_rows_says_whether_their_results_differed(
        home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        first = (await h.call("query", source="shop", question="All", sql=ALL)).ai.structured["pull_id"]
        with sqlite3.connect(shop_db) as db:
            db.execute("INSERT INTO customers VALUES (4, 'Barbara', 'south')")
        db.close()
        second = (await h.call("rerun", pull_id=first)).ai.structured["pull_id"]
        reply = await h.call("compare", pull_id=first, other_pull_id=second)
    result = reply.ai.structured
    assert result["rows_from"] == "rerun"
    assert result["stored_results_differ"] is True
    assert result["stored_row_counts"] == {"before": 3, "after": 4}
    assert "weren't kept" in reply.ai.text and "differed" in reply.ai.text


def change_customers(shop_db: Path, sql: str) -> None:
    with sqlite3.connect(shop_db) as db:
        db.execute(sql)
    db.close()


async def test_when_both_pulls_kept_their_rows_compare_uses_the_rows_as_they_were(
        home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, mode="results") as h:
        await h.call("keep_rows", on=True)
        first = (await h.call("query", source="shop", question="All", sql=ALL)).ai.structured["pull_id"]
        change_customers(shop_db, "INSERT INTO customers VALUES (4, 'Barbara', 'south')")
        second = (await h.call("rerun", pull_id=first)).ai.structured["pull_id"]
        change_customers(shop_db, "DELETE FROM customers WHERE name = 'Ada'")  # after both: a re-run would see it
        reply = await h.call("compare", pull_id=first, other_pull_id=second)
    assert not reply.is_error, reply.ai.text
    result = reply.ai.structured
    assert result["rows_from"] == "kept"
    assert result["added"] == 1 and result["rows"] == [["added", "Barbara", "south"]]
    assert result["removed"] == 0
    assert "as they were" in reply.ai.text and "weren't kept" not in reply.ai.text


async def test_when_only_one_pull_kept_its_rows_compare_re_runs_both(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, mode="results") as h:
        first = (await h.call("query", source="shop", question="All", sql=ALL)).ai.structured["pull_id"]
        await h.call("keep_rows", on=True)
        second = (await h.call("tweak", pull_id=first, sql=NORTH)).ai.structured["pull_id"]
        change_customers(shop_db, "INSERT INTO customers VALUES (4, 'Barbara', 'south')")
        reply = await h.call("compare", pull_id=first, other_pull_id=second)
    result = reply.ai.structured
    assert result["rows_from"] == "rerun"
    assert sorted(result["rows"]) == [["removed", "Barbara", "south"], ["removed", "Grace", "south"]]  # re-run
    assert "re-run now" in reply.ai.text


async def test_kept_rows_that_cant_be_opened_fall_back_to_re_running(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, keychain=MemoryKeychain(), mode="results") as h:
        await h.call("keep_rows", on=True)
        first = (await h.call("query", source="shop", question="All", sql=ALL)).ai.structured["pull_id"]
        second = (await h.call("tweak", pull_id=first, sql=NORTH)).ai.structured["pull_id"]
    async with connected(home, shop_db, name="shop", keychain=MemoryKeychain(), mode="results") as h:  # the key is gone
        reply = await h.call("compare", pull_id=first, other_pull_id=second)
    assert not reply.is_error, reply.ai.text
    assert reply.ai.structured["rows_from"] == "rerun"
    assert reply.ai.structured["rows"] == [["removed", "Grace", "south"]]
    assert "couldn't be opened" in reply.ai.text


async def test_compare_of_an_unknown_pull_is_an_error(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        known = (await h.call("query", source="shop", question="All", sql=ALL)).ai.structured["pull_id"]
        reply = await h.call("compare", pull_id=known, other_pull_id="nope")
    assert reply.is_error and "nope" in reply.ai.text


async def test_lineage_of_an_unknown_pull_is_an_error(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        reply = await h.call("lineage", pull_id="nope")
    assert reply.is_error


async def test_a_tweak_without_a_panel_returns_only_the_capped_preview_and_holds_the_rest(
        home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=False, mode="results") as h:
        original = await h.call("query", source="shop", question="Orders", sql="SELECT id FROM orders ORDER BY id")
        tweak = await h.call("tweak", pull_id=original.ai.structured["pull_id"],
                             sql="SELECT id, total FROM orders ORDER BY id")
        more = await h.call("more_rows", pull_id=tweak.ai.structured["pull_id"], rows=5, approved=False)
    assert not tweak.is_error, tweak.ai.text
    assert tweak.ai.structured["rows"] == [[i, float(i)] for i in range(1, AI_ROWS + 1)]
    assert f"| {AI_ROWS + 1} |" not in tweak.ai.text
    assert f"{ORDER_COUNT} rows" in tweak.ai.text and "more_rows" in tweak.ai.text
    assert "sql_diff" in tweak.ai.structured and tweak.ai.structured["tweak_of"] == original.ai.structured["pull_id"]
    assert not more.is_error and more.ai.structured["rows"] == []  # held, and the user is asked first


ALL_ORDERS = "SELECT id, total FROM orders"
FIRST_HALF = "SELECT id, total FROM orders WHERE id <= 125"


async def _compare_halves(h) -> tuple[object, str]:
    before = (await h.call("query", source="shop", question="Half", sql=FIRST_HALF)).ai.structured["pull_id"]
    after = (await h.call("tweak", pull_id=before, sql=ALL_ORDERS)).ai.structured["pull_id"]
    reply = await h.call("compare", pull_id=before, other_pull_id=after)
    return reply, reply.ai.structured["pull_id"]


async def test_without_a_panel_compare_sends_the_comparison_up_to_the_cap_in_results_mode(
        home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=False, mode="results") as h:
        reply, comparison = await _compare_halves(h)
        more = await h.call("more_rows", pull_id=comparison, rows=500)
    result = reply.ai.structured
    assert not reply.is_error, reply.ai.text
    assert result["added"] == 125 and result["row_count"] == 125 + 3  # 125 added rows, then the 3 total rows
    assert len(result["rows"]) == 128 < AI_ROWS  # it fits under the cap: every row, the totals last
    assert result["rows"][-1][0] == "total change"
    assert "more_rows" not in reply.ai.text
    assert "already been sent" in more.ai.text


async def test_without_a_panel_compare_in_pick_mode_sends_rows_only_on_the_users_yes(
        home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=False, mode="pick") as h:
        reply, comparison = await _compare_halves(h)
    assert reply.ai.structured["rows"] == [] and "total before" not in reply.ai.text and "31375" not in reply.ai.text


async def test_with_a_panel_compare_gives_the_ai_only_the_shape_and_lends_the_rows_to_the_panel(
        home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        reply, comparison = await _compare_halves(h)
        rows = await h.call_from_panel("panel_rows", pull_id=comparison)
    result = reply.ai.structured
    assert not reply.is_error, reply.ai.text
    assert reply.panel is not None  # compare renders in the results panel, like a query
    assert "rows" not in result and result["shown_in"] == "panel"
    assert result["added"] == 125 and result["columns"] == ["change", "id", "total"]
    assert "| 126 |" not in reply.ai.text and "total before" not in reply.ai.text
    assert rows.ai.structured["rows"][0][0] == "added"
    assert rows.ai.structured["rows"][-1][0] == "total change"
