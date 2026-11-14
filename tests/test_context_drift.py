"""Context drift (#123, Personal): on connect, every meaning, definition and link of the profiles applied to a
connection is checked against its catalog. An item naming a column its table no longer has (renamed or dropped) is
held back from the AI and listed for the user on the local Portward page, with a suggested fix; the weekly facts carry
only how many, so the control plane never sees the item. Fixing the item releases it. Driven through the MCP client
harness and the local page, against an in-memory account service."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from test_context import Reviewer, define, draft_meaning, source_entry
from test_local_page import Browser, fetch
from test_profiles import apply, make, seen
from test_why_hidden import lookup, personal

from portward_free import weekly_facts
from portward_free.paths import database_path
from portward_free.sources import Table
from portward_free.storage import LocalStore
from portward_free.why_hidden import Kind

REVENUE = "Revenue is the sum of total over every order"
BIG = "A big order is one whose orders.total is over 15"
TOTAL_MEANING = "What the customer paid, tax included"


def rename_total(db: Path) -> None:
    """The data changes under the profile: orders.total is now orders.order_total."""
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE orders RENAME COLUMN total TO order_total")
    conn.close()


async def stale_context(h) -> None:
    await make(h, "Sales", ["orders.id", "orders.total", "customers.id", "customers.name"])
    await apply(h, "Sales")
    await define(h, "Sales", "revenue", REVENUE)
    await define(h, "Sales", "big order", BIG)
    await h.call("approve_context", draft_id=await draft_meaning(h, "Sales", "orders", TOTAL_MEANING, "total"))
    await define(h, "Sales", "customer", "Anyone in customers with a name")  # names nothing that changes


def held_back_count(home: Path) -> int | None:
    store = LocalStore.open(database_path(home))
    try:
        (connection,) = weekly_facts.facts(store)["connections"]
        return connection.get("held_back")
    finally:
        store.close()


async def test_a_renamed_column_holds_back_every_context_item_naming_it(home: Path, people_db: Path) -> None:
    async with personal(home, people_db) as h:
        await stale_context(h)
        before = await h.call("list_sources")
        rename_total(people_db)
        after = await h.call("list_sources")
        described = await h.call("describe_source", source="people")
        pulled = await h.call("query", source="people", question="What is our revenue?",
                              sql="SELECT sum(order_total) AS revenue FROM orders")
    assert {d["term"] for d in source_entry(before)["definitions"]} == {"revenue", "big order", "customer"}
    assert source_entry(after)["definitions"] == [{"term": "customer", "text": "Anyone in customers with a name"}]
    for reply in (after, described, pulled):
        for text in (REVENUE, BIG, TOTAL_MEANING):
            assert text not in seen(reply)
        assert "held back" not in seen(reply).lower()  # the AI is never told why


async def test_the_lookup_says_the_column_is_gone_and_suggests_the_fix(home: Path, people_db: Path) -> None:
    async with personal(home, people_db) as h:
        await stale_context(h)
    rename_total(people_db)
    catalog = (Table("customers", ("id", "name", "ssn"), schema="main", numeric=frozenset({"id"})),
               Table("orders", ("id", "customer_id", "order_total"), schema="main",
                     numeric=frozenset({"id", "customer_id", "order_total"})),
               Table("notes", ("id", "body"), schema="main", numeric=frozenset({"id"})))
    found = lookup(home, catalog)
    gone = {i.about: i for i in found.items if i.kind is Kind.COLUMN_GONE}
    assert set(gone) == {"definition revenue", "definition big order", "meaning of orders.total"}
    for item in gone.values():
        assert item.names == ("orders.total",)
        assert "orders.order_total" in item.why  # the suggested fix: the column it was most likely renamed to
    assert found.about("customer") == ()


async def test_a_dropped_column_suggests_rewording_with_the_columns_left(home: Path, people_db: Path) -> None:
    async with personal(home, people_db) as h:
        await stale_context(h)
    catalog = (Table("customers", ("id", "name", "ssn"), schema="main"),
               Table("orders", ("id", "customer_id"), schema="main"))
    (item,) = lookup(home, catalog).about("revenue")
    assert item.kind is Kind.COLUMN_GONE
    assert "orders has now (id, customer_id)" in item.why


async def test_a_table_missing_from_this_connection_is_not_drift(home: Path, people_db: Path) -> None:
    # a profile applied to several connections grants tables of each: only a column gone from a table that is
    # here counts
    async with personal(home, people_db) as h:
        await stale_context(h)
    catalog = (Table("customers", ("id", "name", "ssn"), schema="main"),)
    assert [i for i in lookup(home, catalog).items if i.kind is Kind.COLUMN_GONE] == []


async def test_held_back_items_are_listed_on_the_local_page(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await stale_context(h)
        rename_total(people_db)
        await h.call("open_page")
        listed = fetch(browser.url + "hidden?source=people")
        overview = fetch(browser.url)
    assert listed.status == 200
    assert "Needs you" in listed.body and "Column gone" in listed.body
    for about in ("definition revenue", "definition big order", "meaning of orders.total"):
        assert about in listed.body
    assert "orders.order_total" in listed.body
    assert "3 context items held back" in overview.body


async def test_the_weekly_facts_carry_only_a_count(home: Path, people_db: Path) -> None:
    async with personal(home, people_db) as h:
        await stale_context(h)
        await h.call("list_sources")
        assert held_back_count(home) == 0
        rename_total(people_db)
        await h.call("list_sources")
        store = LocalStore.open(database_path(home))
        try:
            report = json.dumps(weekly_facts.facts(store))
        finally:
            store.close()
    assert held_back_count(home) == 3
    for private in ("revenue", "big order", "orders", "order_total", REVENUE, TOTAL_MEANING):
        assert private not in report, private


async def test_fixing_the_item_releases_it(home: Path, people_db: Path) -> None:
    async with personal(home, people_db, reviewer=Reviewer()) as h:
        await stale_context(h)
        rename_total(people_db)
        await h.call("list_sources")
        assert held_back_count(home) == 3
        updated = await h.call("update_profile", profile="Sales", add=["orders.order_total"], remove=["orders.total"])
        assert not updated.is_error, updated.ai.text
        fixed = "Revenue is the sum of order_total over every order"
        await define(h, "Sales", "revenue", fixed)
        listed = await h.call("list_sources")
    assert {"term": "revenue", "text": fixed} in source_entry(listed)["definitions"]
    # removing the stale grant dropped the meaning of orders.total; "big order" still names orders.total
    assert held_back_count(home) == 1
