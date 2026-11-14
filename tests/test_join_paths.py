"""Join paths (#131, Personal): a link says whether it is one-to-one or one-to-many, with match counts measured when it
is saved, and the AI is told to sum the many side before joining a one-to-many so totals don't double-count. Match
counts on anonymised columns are measured on the stand-ins the AI gets; a column the AI only gets totals of has none.
Driven through the MCP client harness."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from test_anonymised import LockedKeyKeychain
from test_anonymised import tool as anonymised_tool
from test_context import Reviewer, source_entry
from test_context_editor import SAVE, open_context, press
from test_local_page import Browser, fetch
from test_profile_editor import follow, personal, stored
from test_links import linked
from test_profiles import apply, make, tool


def more_orders(db: Path) -> None:
    """Ada has three orders, Grace one, Linus none; one order's customer (99) isn't a customer."""
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM orders")
        conn.executemany("INSERT INTO orders VALUES (?, ?, ?)",
                         [(1, 1, 10.0), (2, 1, 20.0), (3, 1, 5.0), (4, 2, 30.0), (5, 99, 1.0)])
    conn.close()


async def test_a_one_to_many_link_tells_the_ai_to_sum_before_joining(home: Path, people_db: Path) -> None:
    more_orders(people_db)
    async with tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["customers.id", "customers.name", "orders.customer_id", "orders.total"])
        await apply(h, "Sales")
        await linked(h, "Sales", "customers.id", "orders.customer_id")
        listed = await h.call("list_sources")
    (link,) = source_entry(listed)["links"]
    assert link["cardinality"] == "one-to-many"
    assert link["counts"] == {"customers.id": {"rows": 3, "values": 3},
                              "orders.customer_id": {"rows": 5, "values": 3}, "matching_values": 2}
    assert "sum or count orders per customer_id before joining" in link["advice"]
    assert link["advice"] in link["join"]


async def test_a_link_saved_in_the_context_editor_is_measured_and_its_counts_shown_there(
        home: Path, people_db: Path) -> None:
    more_orders(people_db)
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.id", "orders.customer_id", "orders.total"])
        await apply(h, "Sales")
        url, page = await open_context(h, browser)
        page = press(url, page, {"to_tab": "links"})
        page = press(url, page, {"action": "add_link"}, link_one="customers.id", link_other="orders.customer_id")
        follow(url, press(url, page, SAVE))
        page = press(url, fetch(url), {"to_tab": "links"})
        listed = await h.call("list_sources")
    (link,) = stored(home, "Sales")[0].context.links
    assert link.cardinality == "one-to-many" and link.counts is not None and link.counts.matching == 2
    assert "one-to-many" in page.body and "2 values match" in page.body
    assert "advice" in source_entry(listed)["links"][0]


async def test_match_counts_on_anonymised_columns_are_measured_on_their_stand_ins(home: Path, people_db: Path) -> None:
    more_orders(people_db)
    async with anonymised_tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["customers.name", "orders.total"], anonymised=["customers.id", "orders.customer_id"])
        await apply(h, "Sales")
        await linked(h, "Sales", "customers.id", "orders.customer_id")
        listed = await h.call("list_sources")
    (link,) = source_entry(listed)["links"]
    assert link["cardinality"] == "one-to-many"
    assert link["counts"]["matching_values"] == 2  # linked stand-ins share a group, so they still match


async def test_without_the_anonymising_key_anonymised_columns_get_no_counts(home: Path, people_db: Path) -> None:
    more_orders(people_db)
    async with anonymised_tool(home, people_db, user=Reviewer(), keychain=LockedKeyKeychain()) as h:
        await make(h, "Sales", ["customers.id", "orders.total"], anonymised=["orders.customer_id"])
        await apply(h, "Sales")
        approved = await linked(h, "Sales", "customers.id", "orders.customer_id")
    (stored,) = approved.ai.structured["profile"]["context"]["links"]
    assert "cardinality" not in stored and "counts" not in stored


async def test_a_column_the_ai_only_gets_totals_of_is_not_measured(home: Path, people_db: Path) -> None:
    more_orders(people_db)
    async with tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["customers.id", "orders.id"], totals=["orders.customer_id"])
        await apply(h, "Sales")
        approved = await linked(h, "Sales", "customers.id", "orders.customer_id")
    (stored,) = approved.ai.structured["profile"]["context"]["links"]
    assert "cardinality" not in stored


async def test_a_drafted_cardinality_is_used_when_nothing_can_be_measured(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["customers.id", "orders.customer_id", "orders.total"])  # not applied anywhere yet
        drafted = await h.call("draft_link", profile="Sales", column="orders.customer_id", other_column="customers.id",
                               cardinality="many-to-one")
        await h.call("approve_context", draft_id=drafted.ai.structured["drafts"][0])
        await apply(h, "Sales")
        listed = await h.call("list_sources")
        wrong = await h.call("draft_link", profile="Sales", column="orders.customer_id", other_column="customers.id",
                             cardinality="lots")
    (link,) = source_entry(listed)["links"]
    assert link["cardinality"] == "many-to-one" and "counts" not in link
    assert "sum or count orders per customer_id before joining" in link["advice"]
    assert wrong.is_error and "one-to-many" in wrong.ai.text


async def test_a_one_to_one_link_gives_no_advice(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:  # one order per customer
        await make(h, "Sales", ["customers.id", "orders.customer_id"])
        await apply(h, "Sales")
        await linked(h, "Sales", "orders.customer_id", "customers.id")
        listed = await h.call("list_sources")
    (link,) = source_entry(listed)["links"]
    assert link["cardinality"] == "one-to-one" and "advice" not in link


async def test_across_connections_each_end_is_counted_in_its_own_connection(home: Path, people_db: Path,
                                                                           tmp_path: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        csv_file = tmp_path / "deals.csv"
        csv_file.write_text("customer_ref,amount\n1,5\n1,6\n2,7\n")
        added = await h.add_file("crm", csv_file, kind="csv")
        assert not added.is_error, added.ai.text
        await make(h, "Sales", ["customers.id", "deals.customer_ref", "deals.amount"])
        await apply(h, "Sales")
        await apply(h, "Sales", source="crm")
        await linked(h, "Sales", "customers.id", "deals.customer_ref")
        listed = await h.call("list_sources")
    (there,) = source_entry(listed, "crm")["links"]
    assert there["cardinality"] == "many-to-one"
    assert there["counts"] == {"deals.customer_ref": {"rows": 3, "values": 2},
                               "customers.id": {"rows": 3, "values": 3}}
    assert "sum or count deals per customer_ref before joining" in there["advice"]
